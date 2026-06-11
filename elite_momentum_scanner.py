#!/usr/bin/env python3
"""
Elite Momentum Scanner
======================
A real-time momentum alert system designed for active stock/options traders.

What it does
------------
- Scans a symbol universe during premarket, regular hours, and postmarket.
- Detects:
  * fast intraday moves
  * unusual relative volume
  * breaks of premarket high / low
  * opening-range breaks
  * trend continuation / flush setups
  * news catalysts (when provider supports it)
- Sends rich Discord alerts.
- Logs alerts to CSV + JSONL.
- Keeps cooldowns so you are not spammed.
- Includes a dry-run / mock test mode.

What it does not do
-------------------
- Place trades
- Guarantee profitable setups
- Replace your chart reading or risk management

Data provider
-------------
This version is built around Alpaca market data + Alpaca news.
You can later swap the provider layer if needed.

Environment variables
---------------------
Required for live mode:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
Optional:
    DISCORD_WEBHOOK_URL

Run examples
------------
    python elite_momentum_scanner.py --mode live
    python elite_momentum_scanner.py --mode dry-run
    python elite_momentum_scanner.py --mode test
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import logging
import os
import random
import platform
import re
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

import requests
from post_alert_performance import PostAlertPerformanceTracker, update_performance_record
from scanner.chop_mode_engine import clean_breakout_exits_chop, evaluate_chop_mode
from scanner.liquidity_sweep_telegram import (
    append_sweep_telegram_log,
    claim_sweep_delivery,
    select_liquidity_sweep_message,
    sweep_telegram_eligibility,
    validate_liquidity_sweep_message,
)
from scanner.missed_clean_entry import detect_missed_clean_entry
from strategies import evaluate_strategy_suite
from strategies.base import ema as strategy_ema, vwap as strategy_vwap
from strategies.context import evaluate_multi_timeframe_context
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

APP_DIR = Path(__file__).resolve().parent
LOG_DIR = APP_DIR / "logs"
STATE_DIR = APP_DIR / "state"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
MARKET_DATA_STATUS_LOG = LOG_DIR / "market_data_status.jsonl"
NOTIFICATION_STATUS_LOG = LOG_DIR / "notification_status.jsonl"
SCANNER_STARTUP_STATUS_LOG = LOG_DIR / "scanner_startup_status.jsonl"
MARKET_REGIME_LOG = LOG_DIR / "market_regime.jsonl"
MULTI_TIMEFRAME_CONTEXT_LOG = LOG_DIR / "multi_timeframe_context.jsonl"
POST_ALERT_PERFORMANCE_LOG = LOG_DIR / "post_alert_performance.jsonl"
NEWS_CONTEXT_LOG = LOG_DIR / "news_context.jsonl"
CHOP_MODE_LOG = LOG_DIR / "chop_mode.jsonl"
MISSED_CLEAN_ENTRY_LOG = LOG_DIR / "missed_clean_entry.jsonl"
PHASE3_TELEGRAM_DEDUPE_STATE = STATE_DIR / "phase3_telegram_dedupe.json"
TELEGRAM_DELIVERY_DEDUPE_STATE = STATE_DIR / "telegram_delivery_dedupe.json"
LIQUIDITY_SWEEP_TELEGRAM_DEDUPE_STATE = STATE_DIR / "liquidity_sweep_telegram_dedupe.json"

logger = logging.getLogger("elite_scanner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ------------------------------------------------------------
# Default config
# ------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "symbols": [
        "AAPL",
    ],
    "scan_interval_seconds": 10,
    "premarket_start": "04:00",
    "market_open": "09:30",
    "market_close": "16:00",
    "postmarket_end": "20:00",
    "lookback_minutes_fast_move": 5,
    "fast_move_pct_threshold": 1.5,
    "day_move_pct_threshold": 4.0,
    "relative_volume_threshold": 3.0,
    "opening_range_minutes": 5,
    "opening_range_minutes_secondary": 15,
    "opening_range_break_buffer_pct": 0.03,
    "opening_range_watch_proximity_pct": 0.12,
    "premarket_watch_proximity_pct": 0.15,
    "alert_cooldown_seconds": 600,
    "max_news_age_minutes": 120,
    "news_context": {
        "enabled": False,
        "watch_symbols": ["AAPL"],
        "lookback_minutes": 120,
        "context_only": True,
    },
    "only_symbols_with_options": True,
    "symbols_with_options": [
        "AAPL"
    ],
    "filters": {
        "min_price": 2.0,
        "max_price": 1000.0,
        "min_day_volume": 100000,
    },
    "alert_rules": {
        "fast_move": True,
        "high_relative_volume": True,
        "premarket_high_break": True,
        "premarket_low_break": True,
        "opening_range_break": True,
        "news_catalyst": True,
    },
    "discord": {
        "enabled": True,
        "mention": "",
    },
    "notifications": {
        "mac_desktop_enabled": True,
        "pushover_enabled": True,
        "messages_enabled": True,
        "messages_watch_enabled": True,
        "telegram_enabled": False,
        "telegram_alert_types": ["PHASE3_HEADS_UP", "STOCK_ONLY_WARNING", "NORMAL_WATCH", "NORMAL_SMS"],
        "telegram_aapl_only": True,
        "telegram_send_test_on_start": False,
        "telegram_timeout_seconds": 8,
        "openai_alert_formatter_enabled": True,
        "openai_alert_formatter_style": "section",
        "openai_alert_formatter_fallback": True,
        "openai_alert_formatter_max_chars": 900,
    },
    "outputs": {
        "csv_log": str(LOG_DIR / "alerts.csv"),
        "jsonl_log": str(LOG_DIR / "alerts.jsonl"),
        "state_file": str(STATE_DIR / "scanner_state.json"),
    },
    "post_alert_performance": {
        "enabled": True,
        "interval_minutes": [1, 3, 5, 10, 15],
        "target_move_pct": 0.30,
    },
    "discovery": {
        "enabled": True,
        "max_universe_symbols": 1200,
        "max_candidates": 100,
        "batch_size": 150,
        "include_etfs": True,
        "include_otc": False,
    },
    "data_quality": {
        "stale_after_minutes": 20,
        "min_recent_bars": 10,
    },
    "market_structure_engines": {
        "enable_support_resistance_engine": True,
        "enable_supply_demand_engine": True,
        "enable_dashboard": True,
        "support_resistance_timeframes": ["1m", "5m", "15m"],
        "supply_demand_timeframes": ["1m", "5m", "15m"],
        "max_levels_per_timeframe": 3,
        "max_zones_per_timeframe": 3,
        "refresh_seconds": 15,
        "min_level_strength": 55,
        "min_zone_strength": 55,
        "can_confirm": True,
        "can_downgrade": True,
        "can_upgrade": False,
        "enable_telegram": True,
        "telegram_max_lines": 2,
    },
    "decision_quality": {
        "enable_chop_mode": True,
        "chop_mode_lookback_minutes": 15,
        "chop_mode_min_flips": 2,
        "chop_mode_min_mixed_alerts": 3,
        "chop_mode_suppress_repeated_alerts": True,
        "chop_mode_cooldown_minutes": 15,
        "chop_mode_allow_breakout_exit": True,
        "enable_missed_clean_entry_label": True,
        "missed_clean_entry_lookback_minutes": 15,
        "missed_clean_entry_cooldown_minutes": 15,
    },
    "liquidity_sweep_engine": {
        "enabled": True,
        "timeframes": ["1m", "5m", "15m"],
        "min_confidence": 55,
        "confirm_on_candle_close": True,
        "watch_distance_bps": 8,
        "cooldown_minutes": 10,
        "use_supply_demand": True,
        "use_support_resistance": True,
        "can_confirm": True,
        "can_downgrade": True,
        "can_upgrade": False,
        "telegram_enabled": True,
        "telegram_watch_enabled": True,
        "telegram_forming_enabled": True,
        "telegram_confirmed_enabled": True,
        "telegram_min_confidence": 55,
        "telegram_confirmed_min_confidence": 65,
        "telegram_cooldown_minutes": 10,
        "telegram_max_chars": 900,
        "telegram_include_structure": True,
    },
    "market_data": {
        "stock_feed": "sip",
        "api_rate_limit_mode": "Algo Trader Plus expected",
        "websocket_symbol_limit": "paid/unlimited expected",
    },
    "alert_quality": {
        "sms_min_grade": "B",
        "min_sms_score": 55,
        "min_sms_options_score": 65,
        "min_sms_rvol": 1.5,
        "max_sms_bar_age_minutes": 2.0,
        "max_sms_option_spread_pct": 8.0,
        "market_alignment_required": True,
        "hold_break_bars": 1,
        "immediate_break_min_distance_pct": 0.08,
        "immediate_break_min_fast_move_pct": 0.15,
        "watch_min_rvol": 0.8,
        "watch_min_score": 35,
        "watch_text_min_rvol": 0.45,
        "watch_text_min_score": 15,
        "watch_text_min_options_score": 60,
        "watch_market_alignment_required": False,
        "watch_block_opposed_market": True,
        "opposed_bearish_watch_enabled": True,
        "opposed_bearish_watch_min_rvol": 0.30,
        "opposed_bearish_watch_min_score": 15,
        "opposed_bearish_watch_min_options_score": 65,
        "opposed_bearish_watch_min_fast_move_pct": 0.08,
        "opposed_bearish_watch_min_day_move_pct": 0.75,
        "opposed_bearish_watch_max_counter_day_move_pct": 2.5,
        "opposed_bearish_watch_max_break_distance_pct": 1.50,
        "sms_symbol_cooldown_seconds": 1200,
        "watch_symbol_cooldown_seconds": 600,
        "strong_fast_break_min_rvol": 1.25,
        "strong_fast_break_min_fast_move_pct": 0.75,
        "allow_unknown_market_for_strong_fast_break": True,
        "trend_flip_after_watch_enabled": True,
        "trend_flip_after_watch_lookback_seconds": 900,
        "trend_flip_after_watch_min_fast_move_pct": 0.12,
        "trend_flip_after_watch_min_rvol": 1.0,
        "failed_breakout_watch_enabled": True,
        "failed_breakout_watch_min_fast_move_pct": 0.12,
        "failed_breakout_watch_lookback_bars": 8,
        "failed_breakout_watch_max_distance_pct": 0.60,
        "block_text_when_fast_move_opposes_setup_pct": 0.08,
        "sustained_trend_watch_enabled": True,
        "sustained_trend_watch_lookback_bars": 12,
        "sustained_trend_watch_min_move_pct": 0.12,
        "sustained_trend_watch_min_index_move_pct": 0.08,
        "sustained_trend_watch_min_rvol": 0.45,
        "sustained_trend_watch_min_green_ratio": 0.58,
        "fast_impulse_watch_enabled": True,
        "fast_impulse_watch_lookback_bars": 3,
        "fast_impulse_watch_min_move_pct": 0.18,
        "fast_impulse_watch_min_index_move_pct": 0.08,
        "fast_impulse_watch_min_rvol": 1.2,
        "fast_impulse_watch_min_aligned_ratio": 0.66,
        "generic_day_conflict_min_day_pct": 1.0,
        "generic_day_conflict_max_fast_pct": 0.25,
        "late_day_repeat_after": "14:30",
        "late_day_generic_min_aligned_fast_move_pct": 0.12,
        "late_day_generic_min_rvol": 3.5,
        "trend_flip_watch_enabled": True,
        "trend_flip_lookback_seconds": 3600,
        "trend_flip_min_rvol": 1.0,
        "trend_flip_min_options_score": 60,
        "max_sms_break_distance_pct": 0.75,
        "max_sms_index_break_distance_pct": 0.30,
        "allow_indicative_sms": True,
        "sms_min_confirmation_score": 60,
        "sms_strong_confirmation_score": 70,
        "sms_block_choppy_market": True,
        "sms_require_candle_alignment": True,
        "sms_require_no_direction_conflict": True,
        "sms_orb_dedupe_minutes": 15,
        "a_plus_min_confirmation_score": 70,
    },
    "strategy_engine": {
        "enabled": True,
        "enable_liquidity_sweep": True,
        "enable_vwap_reclaim": True,
        "enable_opening_range": True,
        "enable_volume_quality": True,
        "enable_candle_strength": True,
        "enable_retest_hold": True,
        "enable_extension_exhaustion": True,
        "enable_relative_strength": True,
        "enable_market_regime": True,
        "enable_pressure_score": False,
        "min_strategy_score_to_alert": 60,
        "sweep_reclaim_candles": 3,
        "volume_confirm_multiplier": 1.5,
        "max_extension_from_vwap_pct": 0.6,
        "max_extension_from_ema9_pct": 0.4,
        "opening_range_minutes_primary": 5,
        "opening_range_minutes_secondary": 15,
    },
    "scenario_engine": {
        "enabled": True,
        "shadow_mode": False,
        "control_dashboard": True,
        "control_sms": False,
        "enable_phase3_heads_up_alerts": True,
        "phase3_heads_up_sms_enabled": True,
        "phase3_heads_up_min_scenario_score": 80,
        "phase3_heads_up_min_stock_score": 65,
        "phase3_heads_up_min_confirmation_score": 55,
        "phase3_good_position_min_scenario_score": 85,
        "phase3_good_position_min_stock_score": 70,
        "phase3_good_position_min_confirmation_score": 60,
        "phase3_heads_up_dedupe_minutes": 15,
        "phase3_heads_up_symbols": ["AAPL"],
        "market_context_symbols": ["SPY", "QQQ"],
        "phase3_late_warning_phone_enabled": False,
        "phase3_late_warning_dedupe_minutes": 30,
        "min_dashboard_score": 55,
        "min_confirmed_score": 70,
        "good_position_score": 75,
        "dedupe_minutes": 10,
        "option_logic_separate_from_stock_setup": True,
        "options_do_not_hide_stock_setups": True,
        "options_block_sms_only": True,
        "opra_unavailable_allow_stock_dashboard": True,
        "opra_unavailable_require_stronger_sms": True,
        "sms_min_stock_setup_score": 70,
        "sms_min_confirmation_score": 60,
        "sms_strong_stock_setup_score": 85,
        "sms_strong_confirmation_score": 70,
        "sms_block_scenario_conflict": True,
        "sms_require_good_stage": True,
    },
    "confirmation": {
        "volume_quality": {
            "enabled": True,
            "rvol_lookback_candles": 20,
            "min_rvol_confirmation": 1.5,
            "strong_rvol_confirmation": 2.0,
            "climax_rvol_multiplier": 3.5,
            "volume_exhaustion_candle_count": 3,
        },
        "candle_strength": {
            "enabled": True,
            "buyer_control_close_top_pct": 25,
            "seller_control_close_bottom_pct": 25,
            "min_body_pct_for_control": 45,
            "large_wick_pct": 40,
            "indecision_body_pct": 25,
        },
        "retest_hold": {
            "enabled": True,
            "retest_lookback_candles": 10,
            "retest_max_distance_from_level_pct": 0.15,
            "retest_confirm_candles": 2,
            "retest_pullback_volume_max_multiplier": 1.2,
        },
        "extension_exhaustion": {
            "enabled": True,
            "max_extension_from_vwap_pct": 0.6,
            "max_extension_from_ema9_pct": 0.4,
            "max_extension_from_key_level_pct": 0.3,
            "consecutive_large_candle_limit": 3,
            "do_not_chase_extension_score": 80,
        },
        "relative_strength": {
            "enabled": True,
            "rs_lookback_candles": 5,
            "rs_strong_diff_pct": 0.20,
            "rs_weak_diff_pct": -0.20,
            "market_confirm_symbols": ["SPY", "QQQ"],
        },
        "market_regime": {
            "enabled": True,
            "market_regime_lookback_candles": 15,
            "choppy_vwap_cross_count": 3,
            "trend_min_score": 65,
        },
        "pressure_score": {
            "enabled": False,
            "pressure_lookback_trades": 50,
            "large_print_multiplier": 3.0,
            "min_pressure_score_confirmation": 60,
            "max_spread_pct": 0.08,
            "enable_quote_imbalance": True,
        },
    },
    "options": {
        "enabled": True,
        "feed": "opra",
        "allow_indicative_fallback": True,
        "expiry_mode": "0dte_then_weekly",
        "delta_min": 0.30,
        "delta_max": 0.60,
        "max_spread_pct": 12,
        "min_option_volume": 100,
        "min_open_interest": 250,
        "block_late_0dte": False,
        "risky_0dte_after_et": "15:30",
        "max_quote_age_seconds": 60,
        "max_chain_contracts": 1000,
    },
}


def git_value(*args: str) -> str:
    try:
        git_dir = APP_DIR / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if args == ("branch", "--show-current"):
            return head.rsplit("/", 1)[-1] if head.startswith("ref: ") else "detached"
        if args == ("rev-parse", "--short", "HEAD"):
            if head.startswith("ref: "):
                ref = head.split(" ", 1)[1]
                ref_path = git_dir / ref
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()[:7]
                for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.endswith(f" {ref}"):
                        return line.split(" ", 1)[0][:7]
            return head[:7]
    except Exception:
        pass
    return "unknown"


def telegram_destination_metadata(chat_id: Optional[str] = None) -> Dict[str, str]:
    value = str(chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    destination_type = "group" if value.startswith("-") else "private" if value else "unknown"
    return {
        "telegram_destination_type": destination_type,
        "telegram_chat_id_last4": value[-4:] if value else "",
    }


def resolve_active_alert_types(config: Optional[Dict[str, Any]] = None) -> List[str]:
    # These are the scanner's supported user-facing alert paths. Telegram route
    # selection remains separately configurable and is not changed here.
    return ["PHASE3_HEADS_UP", "STOCK_ONLY_WARNING", "NORMAL_WATCH", "NORMAL_SMS"]


def scanner_identity(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = config or {}
    return {
        "scanner_instance_name": os.getenv("SCANNER_INSTANCE_NAME", "").strip() or socket.gethostname(),
        "machine_name": os.getenv("SCANNER_INSTANCE_NAME", "").strip() or socket.gethostname(),
        "scanner_machine_role": os.getenv("SCANNER_MACHINE_ROLE", "").strip() or "unspecified",
        "scanner_alert_profile": os.getenv("SCANNER_ALERT_PROFILE", "").strip() or "AAPL_TESTING",
        "hostname": socket.gethostname(),
        "git_commit": git_value("rev-parse", "--short", "HEAD"),
        "git_branch": git_value("branch", "--show-current"),
        "project_path": str(APP_DIR),
        "python_version": platform.python_version(),
        "alert_types_enabled": resolve_active_alert_types(config),
        "alert_symbols": list(config.get("symbols") or ["AAPL"]),
        "context_symbols": list(config.get("scenario_engine", {}).get("market_context_symbols") or ["SPY", "QQQ"]),
        **telegram_destination_metadata(),
    }


# ------------------------------------------------------------
# Models
# ------------------------------------------------------------
@dataclass
class Bar:
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class NewsItem:
    symbol: str
    headline: str
    url: str
    published_at: datetime
    source: str = ""


@dataclass
class OptionContractSnapshot:
    symbol: str
    underlying_symbol: str
    option_type: str
    expiration_date: date
    strike: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    quote_time: Optional[datetime] = None
    trade_time: Optional[datetime] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    delta: Optional[float] = None
    implied_volatility: Optional[float] = None
    feed: str = ""
    is_simulated: bool = False
    quote_raw_data: Dict[str, Any] = field(default_factory=dict)
    quote_raw_type: Optional[str] = None
    quote_timestamp_raw: Optional[str] = None
    quote_timestamp_source_field: Optional[str] = None
    timestamp_available_fields: List[str] = field(default_factory=list)
    timestamp_extraction_failed: bool = False
    timestamp_fallback_used: bool = False
    timestamp_fallback_type: Optional[str] = None
    timestamp_fallback_time: Optional[datetime] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return ((self.ask or 0) - (self.bid or 0)) / mid * 100.0


@dataclass
class OptionSelection:
    contract: Optional[OptionContractSnapshot] = None
    quality: str = "INVALID"
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def is_tradable(self) -> bool:
        return option_quality_is_tradable(self.quality) and self.contract is not None


@dataclass
class SymbolSnapshot:
    symbol: str
    latest_bar: Optional[Bar] = None
    recent_bars: List[Bar] = field(default_factory=list)
    premarket_high: Optional[float] = None
    premarket_low: Optional[float] = None
    opening_range_high: Optional[float] = None
    opening_range_low: Optional[float] = None
    opening_range_15_high: Optional[float] = None
    opening_range_15_low: Optional[float] = None
    daily_bars: List[Bar] = field(default_factory=list)
    multi_timeframe_context: Dict[str, Any] = field(default_factory=dict)
    latest_news: Optional[NewsItem] = None
    best_call: OptionSelection = field(default_factory=OptionSelection)
    best_put: OptionSelection = field(default_factory=OptionSelection)


@dataclass
class Alert:
    symbol: str
    timestamp: datetime
    category: str
    price: float
    fast_move_pct: Optional[float] = None
    day_move_pct: Optional[float] = None
    relative_volume: Optional[float] = None
    premarket_high: Optional[float] = None
    premarket_low: Optional[float] = None
    opening_range_high: Optional[float] = None
    opening_range_low: Optional[float] = None
    headline: Optional[str] = None
    url: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    option_contract: Optional[str] = None
    option_type: Optional[str] = None
    option_expiration: Optional[str] = None
    option_strike: Optional[float] = None
    option_bid: Optional[float] = None
    option_ask: Optional[float] = None
    option_mid: Optional[float] = None
    option_spread_pct: Optional[float] = None
    option_delta: Optional[float] = None
    option_iv: Optional[float] = None
    option_volume: Optional[int] = None
    option_open_interest: Optional[int] = None
    option_quote_timestamp_raw: Optional[str] = None
    option_quote_timestamp_utc: Optional[str] = None
    option_quote_age_seconds: Optional[float] = None
    option_max_quote_age_seconds: Optional[float] = None
    option_stale_reason: Optional[str] = None
    option_data_source: Optional[str] = None
    option_fallback_used: Optional[bool] = None
    option_timestamp_source_field: Optional[str] = None
    option_timestamp_extraction_failed: Optional[bool] = None
    option_timestamp_available_fields: List[str] = field(default_factory=list)
    option_timestamp_fallback_type: Optional[str] = None
    option_fallback_timestamp_utc: Optional[str] = None
    option_quality: Optional[str] = None
    option_quality_message: Optional[str] = None
    option_quality_reasons: List[str] = field(default_factory=list)
    option_days_to_expiration: Optional[int] = None
    option_is_0dte: Optional[bool] = None
    option_strike_distance_pct: Optional[float] = None
    option_liquidity_state: Optional[str] = None
    option_time_state: Optional[str] = None
    option_stock_only_allowed: bool = True
    options_score: Optional[int] = None
    direction: Optional[str] = None
    alert_grade: Optional[str] = None
    alert_score: Optional[int] = None
    alert_tier: Optional[str] = None
    alert_tier_reason: Optional[str] = None
    alert_source: Optional[str] = None
    message_source_path: Optional[str] = None
    phone_conclusion: Optional[str] = None
    phone_conclusion_reason: Optional[str] = None
    plain_english_conclusion: Optional[str] = None
    alert_decision_label: Optional[str] = None
    alert_decision_explanation: Optional[str] = None
    decision_tier: Optional[str] = None
    decision_label: Optional[str] = None
    decision_reason: Optional[str] = None
    internal_risk_warning_reason: Optional[str] = None
    risk_warning_is_actual_risk: bool = False
    no_trade_reason: Optional[str] = None
    mixed_signal_no_trade: bool = False
    telegram_message_version: Optional[str] = None
    old_format_removed: bool = False
    invalidation_level: Optional[float] = None
    invalidation_reason: Optional[str] = None
    stop_logic_description: Optional[str] = None
    pullback_required: bool = False
    do_not_chase_warning: bool = False
    entry_timing_label: Optional[str] = None
    sms_allowed: bool = False
    watch_allowed: bool = False
    sms_sent: Optional[bool] = None
    market_alignment: Optional[str] = None
    text_alert_reason: Optional[str] = None
    setup_level: Optional[str] = None
    trigger_level: Optional[float] = None
    primary_setup: Optional[str] = None
    secondary_setups: List[str] = field(default_factory=list)
    strategy_direction: Optional[str] = None
    strategy_confidence_score: Optional[int] = None
    strategy_confidence_label: Optional[str] = None
    risk_label: Optional[str] = None
    confirmation_score: Optional[int] = None
    confirmation_label: Optional[str] = None
    entry_quality_label: Optional[str] = None
    volume_label: Optional[str] = None
    rvol_detail: Optional[float] = None
    candle_label: Optional[str] = None
    candle_score: Optional[int] = None
    extension_label: Optional[str] = None
    extension_score: Optional[int] = None
    relative_strength_label: Optional[str] = None
    relative_strength_score: Optional[int] = None
    market_regime: Optional[str] = None
    regime_score: Optional[int] = None
    market_score: Optional[int] = None
    regime_reason: Optional[str] = None
    spy_alignment: Optional[str] = None
    qqq_alignment: Optional[str] = None
    aapl_relative_strength: Optional[str] = None
    volume_state: Optional[str] = None
    volatility_state: Optional[str] = None
    trend_1m: Optional[str] = None
    trend_5m: Optional[str] = None
    trend_15m: Optional[str] = None
    daily_trend: Optional[str] = None
    current_structure_bias: Optional[str] = None
    structure_key_warning: Optional[str] = None
    nearest_level_name: Optional[str] = None
    nearest_level_price: Optional[float] = None
    distance_to_key_level_pct: Optional[float] = None
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    demand_zones: List[Dict[str, float]] = field(default_factory=list)
    supply_zones: List[Dict[str, float]] = field(default_factory=list)
    liquidity_above_highs: List[Dict[str, Any]] = field(default_factory=list)
    liquidity_below_lows: List[Dict[str, Any]] = field(default_factory=list)
    multi_timeframe_levels: Dict[str, float] = field(default_factory=dict)
    professional_setup: Dict[str, Any] = field(default_factory=dict)
    setup_name: Optional[str] = None
    setup_code: Optional[str] = None
    setup_direction: Optional[str] = None
    setup_stage: Optional[str] = None
    setup_score: Optional[int] = None
    setup_confidence: Optional[str] = None
    setup_reason: Optional[str] = None
    setup_invalidation_level: Optional[float] = None
    setup_entry_quality: Optional[str] = None
    setup_risk_label: Optional[str] = None
    setup_watch_text: Optional[str] = None
    setup_block_reason: Optional[str] = None
    pressure_label: Optional[str] = None
    pressure_score: Optional[int] = None
    scenario_top: Optional[Dict[str, Any]] = None
    scenario_second: Optional[Dict[str, Any]] = None
    scenario_score: Optional[int] = None
    scenario_stage: Optional[str] = None
    scenario_direction: Optional[str] = None
    scenario_confidence_label: Optional[str] = None
    scenario_entry_quality_label: Optional[str] = None
    scenario_risk_label: Optional[str] = None
    scenario_reasons: List[str] = field(default_factory=list)
    scenario_warnings: List[str] = field(default_factory=list)
    scenario_levels: Dict[str, float] = field(default_factory=dict)
    bullish_score: Optional[int] = None
    bearish_score: Optional[int] = None
    chop_score: Optional[int] = None
    fakeout_score: Optional[int] = None
    scenario_conflict: Optional[bool] = None
    mixed_signal_detected: Optional[bool] = None
    primary_setup_direction: Optional[str] = None
    phase3_scenario_direction: Optional[str] = None
    mixed_signal_reason: Optional[str] = None
    conflict_warning_added: Optional[bool] = None
    news_context_present: Optional[bool] = None
    latest_headline: Optional[str] = None
    news_source: Optional[str] = None
    news_age_minutes: Optional[float] = None
    news_sentiment_guess: Optional[str] = None
    news_used_for_context_only: Optional[bool] = None
    news_upgraded_alert: Optional[bool] = None
    all_scenarios: List[Dict[str, Any]] = field(default_factory=list)
    stock_setup_score: Optional[int] = None
    stock_setup_valid: Optional[bool] = None
    option_tradability_score: Optional[int] = None
    option_feed_status: Optional[str] = None
    option_tradable: Optional[bool] = None
    scenario_alert_eligible: Optional[bool] = None
    scenario_would_sms: Optional[bool] = None
    scenario_alert_tier: Optional[str] = None
    scenario_alert_block_reason: Optional[str] = None
    scenario_sms_block_reason: Optional[str] = None
    sms_allowed_by_stock: Optional[bool] = None
    sms_allowed_by_options: Optional[bool] = None
    sms_block_reason: Optional[str] = None
    scenario_sms_allowed: Optional[bool] = None
    stock_setup_score_reason: Optional[str] = None
    phase3_heads_up_eligible: Optional[bool] = None
    phase3_heads_up_sent: Optional[bool] = None
    phase3_heads_up_block_reason: Optional[str] = None
    phase3_heads_up_type: Optional[str] = None
    phase3_heads_up_dedupe_key: Optional[str] = None
    phase3_heads_up_message_fingerprint: Optional[str] = None
    phase3_heads_up_dedupe_blocked: Optional[bool] = None
    phase3_heads_up_dedupe_reason: Optional[str] = None
    phase3_heads_up_last_sent_time: Optional[str] = None
    phase3_heads_up_next_eligible_time: Optional[str] = None
    phase3_heads_up_dedupe_minutes_remaining: Optional[float] = None
    market_confirmation_status: Optional[str] = None
    context_symbols_available: List[str] = field(default_factory=list)
    phase3_heads_up_message_preview: Optional[str] = None
    stock_only_heads_up_allowed: Optional[bool] = None
    stock_only_heads_up_reason: Optional[str] = None
    phase3_heads_up_final_decision: Optional[str] = None
    phase3_heads_up_final_block_reason: Optional[str] = None
    market_context_missing_warning: Optional[bool] = None
    option_stale_did_not_block_heads_up: Optional[bool] = None
    context_symbols_expected: List[str] = field(default_factory=list)
    watch_only_late_move: Optional[bool] = None
    do_not_chase_watch: Optional[bool] = None
    chop_mode_active: bool = False
    chop_mode_type: Optional[str] = None
    chop_mode_reason: Optional[str] = None
    chop_suppression_active: bool = False
    chop_suppression_reason: Optional[str] = None
    chop_warning_sent: bool = False
    suppressed_by_chop: bool = False
    market_structure_summary: Optional[str] = None
    market_structure_warning: Optional[str] = None
    market_structure_range_low: Optional[float] = None
    market_structure_range_high: Optional[float] = None
    market_structure_near_demand: bool = False
    market_structure_near_supply: bool = False
    missed_clean_entry: bool = False
    previous_clean_setup_time: Optional[str] = None
    previous_clean_setup_name: Optional[str] = None
    previous_clean_setup_score: Optional[int] = None
    missed_clean_entry_reason: Optional[str] = None
    lesson: Optional[str] = None
    bearish_confirmation_quality: Optional[str] = None
    bearish_confirmation_reason: Optional[str] = None
    bearish_downgraded_by_structure: bool = False
    bearish_downgrade_reason: Optional[str] = None
    sweep_risk_active: bool = False
    upside_sweep_zone: Optional[Dict[str, Any]] = None
    downside_sweep_zone: Optional[Dict[str, Any]] = None
    recent_sweep_count: int = 0
    sweep_risk_reason: Optional[str] = None
    downgraded_by_liquidity_sweep: bool = False
    liquidity_sweep_downgrade_reason: Optional[str] = None
    liquidity_sweep_context: Optional[str] = None
    sweep_trap_bias: Optional[str] = None
    strategy_reasons: List[str] = field(default_factory=list)
    strategy_warnings: List[str] = field(default_factory=list)
    strategy_levels: Dict[str, float] = field(default_factory=dict)
    strategy_results: List[Dict[str, Any]] = field(default_factory=list)

    def dedupe_key(self) -> str:
        day_key = self.timestamp.astimezone(ET).strftime("%Y-%m-%d")
        level = self.setup_level or "ALERT"
        return f"{day_key}:{self.symbol}:{level}:{self.category}"

    def short_summary(self) -> str:
        extras: List[str] = []
        if self.fast_move_pct is not None:
            extras.append(f"{self.fast_move_pct:+.2f}% fast")
        if self.day_move_pct is not None:
            extras.append(f"day {self.day_move_pct:+.2f}%")
        if self.relative_volume is not None:
            extras.append(f"RVOL {self.relative_volume:.2f}x")
        if self.alert_grade:
            extras.append(f"grade {self.alert_grade}")
        return f"{self.symbol} | ${self.price:.2f} | {self.category} | " + " | ".join(extras)


# ------------------------------------------------------------
# Utility helpers
# ------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(UTC)


def now_et() -> datetime:
    return now_utc().astimezone(ET)


def parse_hhmm(hhmm: str) -> Tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def set_today_time_et(hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    t = now_et()
    return t.replace(hour=h, minute=m, second=0, microsecond=0)


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100.0


def average(values: Iterable[float]) -> Optional[float]:
    vals = list(values)
    if not vals:
        return None
    return sum(vals) / len(vals)


def first_present(item: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_optional_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw_value = str(value).strip()
        # Alpaca OPRA timestamps can contain nanoseconds. Python versions
        # differ in how many fractional-second digits fromisoformat accepts,
        # so normalize to datetime's microsecond precision before parsing.
        nanosecond_match = re.match(
            r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(Z|[+-]\d{2}:?\d{2})$",
            raw_value,
        )
        if nanosecond_match:
            prefix, fraction, suffix = nanosecond_match.groups()
            raw_value = f"{prefix}.{fraction[:6]}{suffix}"
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    # Alpaca timestamps are UTC. Treat a missing timezone as UTC so local
    # machine timezone settings cannot change quote freshness decisions.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def news_sentiment_guess(headline: Optional[str]) -> str:
    text = str(headline or "").lower()
    positive = ("beats", "raises", "growth", "gain", "record", "upgrade", "approval", "launch", "strong")
    negative = ("misses", "cuts", "decline", "drop", "downgrade", "probe", "lawsuit", "delay", "weak")
    positive_hits = sum(term in text for term in positive)
    negative_hits = sum(term in text for term in negative)
    if positive_hits > negative_hits:
        return "POSITIVE"
    if negative_hits > positive_hits:
        return "NEGATIVE"
    return "NEUTRAL"


def safe_market_data_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method()
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
    raw = getattr(value, "raw_data", None) or getattr(value, "raw", None)
    if isinstance(raw, dict):
        return dict(raw)
    attrs = getattr(value, "__dict__", None)
    return dict(attrs) if isinstance(attrs, dict) else {}


def extract_quote_timestamp(quote: Any, symbol: Optional[str] = None) -> Dict[str, Any]:
    candidate_names = ("timestamp", "t", "time", "updated_at", "created_at", "quote_timestamp")
    available_fields: List[str] = []
    visited: set[int] = set()

    def walk(value: Any, path: str = "quote", depth: int = 0) -> Tuple[Any, Optional[str]]:
        if value is None or depth > 4 or id(value) in visited:
            return None, None
        visited.add(id(value))
        mapping = safe_market_data_mapping(value)
        available_fields.extend(f"{path}.{key}" for key in mapping.keys())
        for name in candidate_names:
            # Prefer direct mapping access for Alpaca REST quote dictionaries.
            # Timestamp field discovery must not depend on parsing succeeding.
            candidate = mapping.get(name) if name in mapping else getattr(value, name, None)
            if candidate is not None and candidate != "":
                return candidate, f"{path}.{name}"
        nested_names = ("quotes", "latestQuote", "latest_quote", "quote", "raw", "raw_data")
        if symbol:
            nested_names = (*nested_names, symbol)
        for name in nested_names:
            nested = mapping.get(name)
            if nested is None:
                nested = getattr(value, name, None)
            if nested is not None:
                candidate, source = walk(nested, f"{path}.{name}", depth + 1)
                if source:
                    return candidate, source
        if symbol and symbol in mapping:
            return walk(mapping[symbol], f"{path}.{symbol}", depth + 1)
        return None, None

    raw, source = walk(quote)
    parsed = parse_optional_dt(raw)
    return {
        "quote_timestamp_raw": str(raw) if raw is not None else None,
        "quote_timestamp_utc": parsed,
        "timestamp_source_field": source,
        "timestamp_extraction_failed": parsed is None,
        "timestamp_available_fields": sorted(set(available_fields)),
        "safe_raw_data": safe_market_data_mapping(quote),
    }


def parse_option_symbol(contract_symbol: str, underlying_symbol: str) -> Optional[Tuple[str, date, float]]:
    if not contract_symbol.startswith(underlying_symbol):
        return None
    tail = contract_symbol[len(underlying_symbol):]
    if len(tail) < 15:
        return None
    yymmdd = tail[:6]
    option_type = tail[6].upper()
    strike_raw = tail[7:]
    if option_type not in {"C", "P"} or not yymmdd.isdigit() or not strike_raw.isdigit():
        return None
    try:
        expiration = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return option_type, expiration, int(strike_raw) / 1000.0


def option_symbol(underlying: str, expiration: date, option_type: str, strike: float) -> str:
    return f"{underlying}{expiration.strftime('%y%m%d')}{option_type}{int(round(strike * 1000)):08d}"


def load_dotenv(path: Path = APP_DIR / ".env") -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def normalize_feed(value: Any, default: str) -> str:
    feed = str(value or default).strip().lower()
    return feed or default


def stock_feed_from_config(config: Dict[str, Any]) -> str:
    return normalize_feed(config.get("market_data", {}).get("stock_feed"), "sip")


def options_feed_from_config(config: Dict[str, Any]) -> str:
    return normalize_feed(config.get("options", {}).get("feed"), "opra")


def opra_agreement_error(text: str) -> bool:
    lower = text.lower()
    return "opra" in lower and ("agreement" in lower or "not signed" in lower or "subscription" in lower)


def append_market_data_status(payload: Dict[str, Any]) -> None:
    MARKET_DATA_STATUS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MARKET_DATA_STATUS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def latest_market_data_status(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = config or DEFAULT_CONFIG
    fallback = bool(config.get("options", {}).get("allow_indicative_fallback", True))
    base = {
        "timestamp": None,
        "stock_feed_requested": stock_feed_from_config(config).upper(),
        "stock_feed_status": "unknown",
        "options_feed_requested": options_feed_from_config(config).upper(),
        "options_feed_status": "unknown",
        "opra_status": "unknown",
        "api_rate_limit_mode": config.get("market_data", {}).get("api_rate_limit_mode", "Algo Trader Plus expected"),
        "websocket_symbol_limit": config.get("market_data", {}).get("websocket_symbol_limit", "paid/unlimited expected"),
        "allow_indicative_options_fallback": fallback,
        "last_data_check_time": None,
        "feed_warning": "",
    }
    if not MARKET_DATA_STATUS_LOG.exists():
        return base
    try:
        last_line = ""
        with MARKET_DATA_STATUS_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last_line = line
        if not last_line:
            return base
        loaded = json.loads(last_line)
        if isinstance(loaded, dict):
            base.update(loaded)
    except Exception as exc:
        base["feed_warning"] = f"Could not read market data status log: {exc}"
    return base


# ------------------------------------------------------------
# Data provider protocol
# ------------------------------------------------------------
class DataProvider(Protocol):
    def get_latest_bars(self, symbols: List[str]) -> Dict[str, Bar]: ...
    def get_recent_bars(self, symbols: List[str], start: datetime, end: datetime) -> Dict[str, List[Bar]]: ...
    def get_daily_bars(self, symbols: List[str], start: datetime, end: datetime) -> Dict[str, List[Bar]]: ...
    def get_news(self, symbols: List[str], limit: int = 50) -> List[NewsItem]: ...
    def discover_symbols(self, config: Dict[str, Any]) -> List[str]: ...
    def get_option_chain(self, symbol: str, config: Dict[str, Any]) -> List[OptionContractSnapshot]: ...


# ------------------------------------------------------------
# Alpaca live provider
# ------------------------------------------------------------
class AlpacaProvider:
    def __init__(self, api_key: str, secret_key: str, feed: str = "sip") -> None:
        self.feed = normalize_feed(feed, "sip")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
            }
        )
        self.base_v2 = "https://data.alpaca.markets/v2"
        self.base_v1beta = "https://data.alpaca.markets/v1beta1"
        self.asset_bases = [
            "https://paper-api.alpaca.markets/v2",
            "https://api.alpaca.markets/v2",
        ]
        self._asset_symbol_cache: Optional[List[str]] = None

    def get_latest_bars(self, symbols: List[str]) -> Dict[str, Bar]:
        resp = self.session.get(
            f"{self.base_v2}/stocks/bars/latest",
            params={"symbols": ",".join(symbols), "feed": self.feed},
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("bars", {})
        out: Dict[str, Bar] = {}
        for symbol, item in raw.items():
            out[symbol] = self._bar_from_json(item)
        return out

    def get_recent_bars(self, symbols: List[str], start: datetime, end: datetime) -> Dict[str, List[Bar]]:
        resp = self.session.get(
            f"{self.base_v2}/stocks/bars",
            params={
                "symbols": ",".join(symbols),
                "timeframe": "1Min",
                "start": start.astimezone(UTC).isoformat(),
                "end": end.astimezone(UTC).isoformat(),
                "adjustment": "raw",
                "feed": self.feed,
                "limit": 10000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json().get("bars", {})
        out: Dict[str, List[Bar]] = {}
        for symbol, items in raw.items():
            out[symbol] = [self._bar_from_json(item) for item in items]
        return out

    def get_daily_bars(self, symbols: List[str], start: datetime, end: datetime) -> Dict[str, List[Bar]]:
        resp = self.session.get(
            f"{self.base_v2}/stocks/bars",
            params={
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start.astimezone(UTC).isoformat(),
                "end": end.astimezone(UTC).isoformat(),
                "adjustment": "raw",
                "feed": self.feed,
                "limit": 1000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("bars", {})
        return {symbol: [self._bar_from_json(item) for item in items] for symbol, items in raw.items()}

    def get_news(self, symbols: List[str], limit: int = 50) -> List[NewsItem]:
        resp = self.session.get(
            f"{self.base_v1beta}/news",
            params={"symbols": ",".join(symbols), "limit": limit, "sort": "desc"},
            timeout=30,
        )
        if resp.status_code >= 400:
            logger.warning("News fetch failed: %s %s", resp.status_code, resp.text[:200])
            return []
        raw = resp.json().get("news", [])
        out: List[NewsItem] = []
        for item in raw:
            syms = item.get("symbols") or []
            for sym in syms:
                if sym in symbols:
                    out.append(
                        NewsItem(
                            symbol=sym,
                            headline=item.get("headline", ""),
                            url=item.get("url", ""),
                            published_at=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
                            source=item.get("source", ""),
                        )
                    )
        return out

    def discover_symbols(self, config: Dict[str, Any]) -> List[str]:
        if self._asset_symbol_cache is not None:
            return list(self._asset_symbol_cache)

        discovery = config.get("discovery", {})
        include_otc = bool(discovery.get("include_otc", False))
        max_universe = int(discovery.get("max_universe_symbols", 1200))
        assets: List[Dict[str, Any]] = []
        last_error = ""

        for base in self.asset_bases:
            resp = self.session.get(
                f"{base}/assets",
                params={"status": "active", "asset_class": "us_equity"},
                timeout=30,
            )
            if resp.status_code < 400:
                assets = resp.json()
                break
            last_error = f"{resp.status_code} {resp.text[:160]}"

        if not assets:
            logger.warning("Asset discovery failed: %s", last_error or "no assets returned")
            return []

        symbols: List[str] = []
        for asset in assets:
            symbol = str(asset.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if asset.get("status") != "active" or not asset.get("tradable", False):
                continue
            exchange = str(asset.get("exchange", "")).upper()
            if not include_otc and exchange == "OTC":
                continue
            if "/" in symbol or "." in symbol or " " in symbol:
                continue
            symbols.append(symbol)
            if len(symbols) >= max_universe:
                break

        self._asset_symbol_cache = symbols
        return list(symbols)

    def get_option_chain(self, symbol: str, config: Dict[str, Any]) -> List[OptionContractSnapshot]:
        options_config = config.get("options", {})
        if not options_config.get("enabled", True):
            return []

        preferred_feed = options_feed_from_config(config)
        limit = int(options_config.get("max_chain_contracts", 1000))
        snapshots: List[OptionContractSnapshot] = []
        feeds_to_try = [preferred_feed]
        if preferred_feed == "opra" and bool(options_config.get("allow_indicative_fallback", True)):
            feeds_to_try.append("indicative")

        for feed in feeds_to_try:
            token: Optional[str] = None
            snapshots = []
            while True:
                today = now_et().date()
                params: Dict[str, Any] = {
                    "feed": feed,
                    "limit": limit,
                    "expiration_date_gte": today.isoformat(),
                    "expiration_date_lte": (today + timedelta(days=7)).isoformat(),
                }
                if token:
                    params["page_token"] = token
                resp = self.session.get(
                    f"{self.base_v1beta}/options/snapshots/{symbol}",
                    params=params,
                    timeout=30,
                )
                if resp.status_code >= 400:
                    logger.warning("Option chain fetch failed for %s using %s: %s %s", symbol, feed, resp.status_code, resp.text[:200])
                    if feed == "opra" and opra_agreement_error(resp.text):
                        logger.warning("OPRA agreement not signed — sign Alpaca OPRA agreement. Options remain indicative.")
                    break

                body = resp.json()
                raw_snapshots = body.get("snapshots", body)
                for contract_symbol, item in raw_snapshots.items():
                    parsed = parse_option_symbol(contract_symbol, symbol)
                    if not parsed:
                        continue
                    option_type, expiration_date, strike = parsed
                    quote = item.get("latestQuote") or item.get("latest_quote") or item.get("q") or {}
                    trade = item.get("latestTrade") or item.get("latest_trade") or item.get("t") or {}
                    greeks = item.get("greeks") or {}
                    quote_timestamp = extract_quote_timestamp(quote, contract_symbol)
                    trade_timestamp = extract_quote_timestamp(trade, contract_symbol)
                    quote_data = safe_market_data_mapping(quote)
                    trade_data = safe_market_data_mapping(trade)
                    snapshots.append(
                        OptionContractSnapshot(
                            symbol=contract_symbol,
                            underlying_symbol=symbol,
                            option_type=option_type,
                            expiration_date=expiration_date,
                            strike=strike,
                            bid=optional_float(first_present(quote_data, ["bp", "bid_price", "bidPrice", "bid"])),
                            ask=optional_float(first_present(quote_data, ["ap", "ask_price", "askPrice", "ask"])),
                            last=optional_float(first_present(trade_data, ["p", "price"])),
                            quote_time=quote_timestamp["quote_timestamp_utc"],
                            trade_time=trade_timestamp["quote_timestamp_utc"],
                            volume=optional_int(first_present(item, ["volume", "day_volume", "dailyVolume"])),
                            open_interest=optional_int(first_present(item, ["open_interest", "openInterest"])),
                            delta=optional_float(first_present(greeks, ["delta"])),
                            implied_volatility=optional_float(first_present(item, ["impliedVolatility", "implied_volatility", "iv"])),
                            feed=feed,
                            quote_raw_data=quote_timestamp["safe_raw_data"],
                            quote_raw_type=type(quote).__name__,
                            quote_timestamp_raw=quote_timestamp["quote_timestamp_raw"],
                            quote_timestamp_source_field=quote_timestamp["timestamp_source_field"],
                            timestamp_available_fields=quote_timestamp["timestamp_available_fields"],
                            timestamp_extraction_failed=quote_timestamp["timestamp_extraction_failed"],
                            timestamp_fallback_used=bool(
                                quote_timestamp["timestamp_extraction_failed"]
                                and trade_timestamp["quote_timestamp_utc"]
                            ),
                            timestamp_fallback_type=(
                                "latest_trade"
                                if quote_timestamp["timestamp_extraction_failed"]
                                and trade_timestamp["quote_timestamp_utc"]
                                else None
                            ),
                            timestamp_fallback_time=(
                                trade_timestamp["quote_timestamp_utc"]
                                if quote_timestamp["timestamp_extraction_failed"]
                                else None
                            ),
                        )
                    )

                token = body.get("next_page_token") or body.get("nextPageToken")
                if not token:
                    break

            if snapshots or feed != preferred_feed:
                return snapshots

        return []

    def check_market_data_status(self, config: Dict[str, Any], symbol: str = "AAPL") -> Dict[str, Any]:
        options_config = config.get("options", {})
        stock_feed = stock_feed_from_config(config)
        options_feed = options_feed_from_config(config)
        fallback_enabled = bool(options_config.get("allow_indicative_fallback", True))
        checked_at = now_utc().isoformat()
        status: Dict[str, Any] = {
            "timestamp": checked_at,
            "last_data_check_time": checked_at,
            "symbol": symbol,
            "stock_feed_requested": stock_feed.upper(),
            "stock_feed_status": "unknown",
            "options_feed_requested": options_feed.upper(),
            "options_feed_status": "unknown",
            "opra_status": "unknown",
            "api_rate_limit_mode": config.get("market_data", {}).get("api_rate_limit_mode", "Algo Trader Plus expected"),
            "websocket_symbol_limit": config.get("market_data", {}).get("websocket_symbol_limit", "paid/unlimited expected"),
            "allow_indicative_options_fallback": fallback_enabled,
            "feed_warning": "",
        }

        try:
            stock = self.session.get(
                f"{self.base_v2}/stocks/bars/latest",
                params={"symbols": symbol, "feed": stock_feed},
                timeout=15,
            )
            if stock.status_code < 400:
                status["stock_feed_status"] = stock_feed.upper()
            else:
                status["stock_feed_status"] = "unavailable"
                status["feed_warning"] = f"Stock {stock_feed.upper()} check failed: {stock.status_code} {stock.text[:160]}"
        except Exception as exc:
            status["stock_feed_status"] = "unavailable"
            status["feed_warning"] = f"Stock {stock_feed.upper()} check failed: {exc}"

        if not options_config.get("enabled", True):
            status["options_feed_status"] = "disabled"
            status["opra_status"] = "disabled"
            return status

        today = now_et().date()
        opra_error = ""
        try:
            opt = self.session.get(
                f"{self.base_v1beta}/options/snapshots/{symbol}",
                params={
                    "feed": options_feed,
                    "limit": 1,
                    "expiration_date_gte": today.isoformat(),
                    "expiration_date_lte": (today + timedelta(days=7)).isoformat(),
                },
                timeout=15,
            )
            if opt.status_code < 400:
                status["options_feed_status"] = options_feed.upper()
                status["opra_status"] = "enabled" if options_feed == "opra" else "not_requested"
                return status
            opra_error = f"{opt.status_code} {opt.text[:200]}"
            if options_feed == "opra" and opra_agreement_error(opt.text):
                status["opra_status"] = "agreement missing"
                status["feed_warning"] = "OPRA agreement not signed — sign Alpaca OPRA agreement. Options remain indicative."
            else:
                status["opra_status"] = "unavailable" if options_feed == "opra" else "not_requested"
                status["feed_warning"] = f"Options {options_feed.upper()} check failed: {opra_error}"
        except Exception as exc:
            opra_error = str(exc)
            status["opra_status"] = "unavailable" if options_feed == "opra" else "not_requested"
            status["feed_warning"] = f"Options {options_feed.upper()} check failed: {exc}"

        if options_feed == "opra" and fallback_enabled:
            try:
                indicative = self.session.get(
                    f"{self.base_v1beta}/options/snapshots/{symbol}",
                    params={
                        "feed": "indicative",
                        "limit": 1,
                        "expiration_date_gte": today.isoformat(),
                        "expiration_date_lte": (today + timedelta(days=7)).isoformat(),
                    },
                    timeout=15,
                )
                if indicative.status_code < 400:
                    status["options_feed_status"] = "INDICATIVE"
                    if not status["feed_warning"]:
                        status["feed_warning"] = "OPRA unavailable — options remain indicative."
                else:
                    status["options_feed_status"] = "unavailable"
                    if not status["feed_warning"]:
                        status["feed_warning"] = f"Indicative options fallback failed: {indicative.status_code} {indicative.text[:160]}"
            except Exception as exc:
                status["options_feed_status"] = "unavailable"
                if not status["feed_warning"]:
                    status["feed_warning"] = f"Indicative options fallback failed: {exc}"
        elif options_feed == "opra":
            status["options_feed_status"] = "unavailable"
            if not status["feed_warning"]:
                status["feed_warning"] = f"OPRA unavailable and indicative fallback disabled: {opra_error}"

        return status

    @staticmethod
    def _bar_from_json(item: Dict[str, Any]) -> Bar:
        return Bar(
            t=datetime.fromisoformat(item["t"].replace("Z", "+00:00")),
            o=float(item["o"]),
            h=float(item["h"]),
            l=float(item["l"]),
            c=float(item["c"]),
            v=float(item["v"]),
        )


# ------------------------------------------------------------
# Mock provider for dry-run / tests
# ------------------------------------------------------------
class MockProvider:
    """Deterministic-enough mock provider so you can test the full system without API keys."""
    def __init__(self, symbols: List[str]) -> None:
        self.symbols = symbols
        self._base_prices = {s: 100.0 + i * 5 for i, s in enumerate(symbols)}
        self._tick = 0

    def _base_price(self, symbol: str) -> float:
        if symbol not in self._base_prices:
            self._base_prices[symbol] = 80.0 + len(self._base_prices) * 4
        return self._base_prices[symbol]

    def get_latest_bars(self, symbols: List[str]) -> Dict[str, Bar]:
        self._tick += 1
        t = now_utc()
        out: Dict[str, Bar] = {}
        for idx, s in enumerate(symbols):
            base = self._base_price(s)
            drift = mathish_wave(self._tick + idx) + random.uniform(-0.3, 0.3)
            if s == "ASTS" and self._tick % 4 == 0:
                drift += 5.0
            price = max(1.0, base + drift)
            out[s] = Bar(t=t, o=price - 0.4, h=price + 0.5, l=price - 0.8, c=price, v=100000 + random.randint(0, 500000))
        return out

    def get_recent_bars(self, symbols: List[str], start: datetime, end: datetime) -> Dict[str, List[Bar]]:
        count = max(10, int((end - start).total_seconds() // 60))
        out: Dict[str, List[Bar]] = {}
        for idx, s in enumerate(symbols):
            base = self._base_price(s)
            bars: List[Bar] = []
            for i in range(count):
                t = start + timedelta(minutes=i)
                drift = mathish_wave(i + idx) + random.uniform(-0.2, 0.2)
                if s == "ASTS" and i > count - 4:
                    drift += 4.0
                price = max(1.0, base + drift)
                bars.append(Bar(t=t, o=price - 0.2, h=price + 0.3, l=price - 0.4, c=price, v=50000 + random.randint(0, 300000)))
            out[s] = bars
        return out

    def get_daily_bars(self, symbols: List[str], start: datetime, end: datetime) -> Dict[str, List[Bar]]:
        out: Dict[str, List[Bar]] = {}
        for idx, symbol in enumerate(symbols):
            base = self._base_price(symbol)
            out[symbol] = [
                Bar(
                    t=(now_utc() - timedelta(days=day)).replace(hour=21, minute=0, second=0, microsecond=0),
                    o=base - day * 0.2,
                    h=base + 1.0 - day * 0.2,
                    l=base - 1.0 - day * 0.2,
                    c=base + 0.2 - day * 0.2,
                    v=5_000_000 + idx * 100_000,
                )
                for day in range(5, 0, -1)
            ]
        return out

    def get_news(self, symbols: List[str], limit: int = 50) -> List[NewsItem]:
        items: List[NewsItem] = []
        if "ASTS" in symbols:
            items.append(
                NewsItem(
                    symbol="ASTS",
                    headline="ASTS gains on satellite network catalyst",
                    url="https://example.com/asts-news",
                    published_at=now_utc() - timedelta(minutes=30),
                    source="MockNews",
                )
            )
        return items

    def discover_symbols(self, config: Dict[str, Any]) -> List[str]:
        symbols = list(dict.fromkeys(self.symbols + ["COIN", "MARA", "RIVN", "SOFI", "HOOD", "IONQ", "BBAI"]))
        return symbols[: int(config.get("discovery", {}).get("max_universe_symbols", len(symbols)))]

    def get_option_chain(self, symbol: str, config: Dict[str, Any]) -> List[OptionContractSnapshot]:
        options_config = config.get("options", {})
        if not options_config.get("enabled", True):
            return []
        base = self._base_price(symbol)
        today = now_et().date()
        expirations = [today, today + timedelta(days=7)]
        chain: List[OptionContractSnapshot] = []
        for expiration in expirations:
            for offset in range(-3, 4):
                strike = round(base + offset * 2.5, 2)
                distance = abs(strike - base)
                for option_type in ("C", "P"):
                    delta_seed = max(0.20, min(0.75, 0.55 - distance / max(base, 1) * 2))
                    delta = delta_seed if option_type == "C" else -delta_seed
                    mid = max(0.35, 3.0 - distance * 0.25)
                    spread = mid * 0.06
                    chain.append(
                        OptionContractSnapshot(
                            symbol=option_symbol(symbol, expiration, option_type, strike),
                            underlying_symbol=symbol,
                            option_type=option_type,
                            expiration_date=expiration,
                            strike=strike,
                            bid=round(mid - spread / 2, 2),
                            ask=round(mid + spread / 2, 2),
                            last=round(mid, 2),
                            quote_time=now_utc(),
                            trade_time=now_utc(),
                            volume=250 + random.randint(0, 500),
                            open_interest=600 + random.randint(0, 1500),
                            delta=delta,
                            implied_volatility=0.45 + random.random() * 0.25,
                            feed="simulated",
                            is_simulated=True,
                        )
                    )
        return chain


def mathish_wave(x: int) -> float:
    # cheap wave without importing math; deterministic enough for dry runs
    return ((x % 13) - 6) * 0.25


# ------------------------------------------------------------
# State persistence
# ------------------------------------------------------------
class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: Dict[str, Any] = {"last_alert_times": {}}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception as exc:
                logger.warning("Could not load state file: %s", exc)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))

    def get_last_alert_time(self, key: str) -> Optional[datetime]:
        raw = self.data.get("last_alert_times", {}).get(key)
        if not raw:
            return None
        return datetime.fromisoformat(raw)

    def set_last_alert_time(self, key: str, dt: datetime) -> None:
        self.data.setdefault("last_alert_times", {})[key] = dt.astimezone(UTC).isoformat()

    def get_phase3_heads_up_record(self, symbol: str) -> Optional[Dict[str, Any]]:
        record = self.data.get("phase3_heads_up_records", {}).get(symbol.upper())
        return dict(record) if isinstance(record, dict) else None

    def set_phase3_heads_up_record(self, alert: Alert, dt: Optional[datetime] = None) -> None:
        self.data.setdefault("phase3_heads_up_records", {})[alert.symbol.upper()] = phase3_heads_up_record(alert, dt)


# ------------------------------------------------------------
# Logging outputs
# ------------------------------------------------------------
class AlertWriter:
    def __init__(self, csv_path: Path, jsonl_path: Path) -> None:
        self.csv_path = csv_path
        self.jsonl_path = jsonl_path
        self.scenario_jsonl_path = LOG_DIR / "scenario_engine.jsonl"
        self.option_jsonl_path = LOG_DIR / "option_quality_decisions.jsonl"
        self.phase3_heads_up_jsonl_path = LOG_DIR / "phase3_heads_up.jsonl"
        self.market_regime_jsonl_path = MARKET_REGIME_LOG
        self.multi_timeframe_jsonl_path = MULTI_TIMEFRAME_CONTEXT_LOG
        self.news_context_jsonl_path = NEWS_CONTEXT_LOG
        self._ensure_csv_header()

    def _ensure_csv_header(self) -> None:
        if self.csv_path.exists():
            return
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "symbol",
                "category",
                "price",
                "fast_move_pct",
                "day_move_pct",
                "relative_volume",
                "premarket_high",
                "premarket_low",
                "opening_range_high",
                "opening_range_low",
                "headline",
                "url",
                "option_contract",
                "option_type",
                "option_expiration",
                "option_strike",
                "option_bid",
                "option_ask",
                "option_mid",
                "option_spread_pct",
                "option_delta",
                "option_iv",
                "option_volume",
                "option_open_interest",
                "option_quality",
                "options_score",
                "direction",
                "alert_grade",
                "alert_score",
                "sms_allowed",
                "watch_allowed",
                "market_alignment",
                "text_alert_reason",
                "setup_level",
                "trigger_level",
                "primary_setup",
                "secondary_setups",
                "strategy_confidence_score",
                "strategy_confidence_label",
                "risk_label",
                "confirmation_score",
                "confirmation_label",
                "entry_quality_label",
                "volume_label",
                "rvol_detail",
                "candle_label",
                "candle_score",
                "extension_label",
                "extension_score",
                "relative_strength_label",
                "relative_strength_score",
                "market_regime",
                "market_score",
                "pressure_label",
                "pressure_score",
                "scenario_top",
                "scenario_second",
                "scenario_score",
                "scenario_stage",
                "scenario_direction",
                "scenario_confidence_label",
                "scenario_entry_quality_label",
                "scenario_risk_label",
                "scenario_reasons",
                "scenario_warnings",
                "scenario_levels",
                "bullish_score",
                "bearish_score",
                "chop_score",
                "fakeout_score",
                "scenario_conflict",
                "all_scenarios",
                "stock_setup_score",
                "stock_setup_valid",
                "option_tradability_score",
                "option_feed_status",
                "option_tradable",
                "scenario_alert_eligible",
                "scenario_would_sms",
                "scenario_alert_tier",
                "scenario_alert_block_reason",
                "sms_allowed_by_stock",
                "sms_allowed_by_options",
                "sms_block_reason",
                "scenario_sms_allowed",
                "scenario_sms_block_reason",
                "stock_setup_score_reason",
                "phase3_heads_up_eligible",
                "phase3_heads_up_sent",
                "phase3_heads_up_block_reason",
                "phase3_heads_up_type",
                "phase3_heads_up_dedupe_key",
                "phase3_heads_up_dedupe_blocked",
                "phase3_heads_up_last_sent_time",
                "phase3_heads_up_next_eligible_time",
                "market_confirmation_status",
                "context_symbols_available",
                "strategy_reasons",
                "strategy_warnings",
                "strategy_levels",
                "notes",
            ])

    def write(self, alert: Alert) -> None:
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                alert.timestamp.isoformat(),
                alert.symbol,
                alert.category,
                alert.price,
                alert.fast_move_pct,
                alert.day_move_pct,
                alert.relative_volume,
                alert.premarket_high,
                alert.premarket_low,
                alert.opening_range_high,
                alert.opening_range_low,
                alert.headline,
                alert.url,
                alert.option_contract,
                alert.option_type,
                alert.option_expiration,
                alert.option_strike,
                alert.option_bid,
                alert.option_ask,
                alert.option_mid,
                alert.option_spread_pct,
                alert.option_delta,
                alert.option_iv,
                alert.option_volume,
                alert.option_open_interest,
                alert.option_quality,
                alert.options_score,
                alert.direction,
                alert.alert_grade,
                alert.alert_score,
                alert.sms_allowed,
                alert.watch_allowed,
                alert.market_alignment,
                alert.text_alert_reason,
                alert.setup_level,
                alert.trigger_level,
                alert.primary_setup,
                " | ".join(alert.secondary_setups),
                alert.strategy_confidence_score,
                alert.strategy_confidence_label,
                alert.risk_label,
                alert.confirmation_score,
                alert.confirmation_label,
                alert.entry_quality_label,
                alert.volume_label,
                alert.rvol_detail,
                alert.candle_label,
                alert.candle_score,
                alert.extension_label,
                alert.extension_score,
                alert.relative_strength_label,
                alert.relative_strength_score,
                alert.market_regime,
                alert.market_score,
                alert.pressure_label,
                alert.pressure_score,
                json.dumps(alert.scenario_top, sort_keys=True) if alert.scenario_top else None,
                json.dumps(alert.scenario_second, sort_keys=True) if alert.scenario_second else None,
                alert.scenario_score,
                alert.scenario_stage,
                alert.scenario_direction,
                alert.scenario_confidence_label,
                alert.scenario_entry_quality_label,
                alert.scenario_risk_label,
                " | ".join(alert.scenario_reasons),
                " | ".join(alert.scenario_warnings),
                json.dumps(alert.scenario_levels, sort_keys=True),
                alert.bullish_score,
                alert.bearish_score,
                alert.chop_score,
                alert.fakeout_score,
                alert.scenario_conflict,
                json.dumps(alert.all_scenarios, sort_keys=True),
                alert.stock_setup_score,
                alert.stock_setup_valid,
                alert.option_tradability_score,
                alert.option_feed_status,
                alert.option_tradable,
                alert.scenario_alert_eligible,
                alert.scenario_would_sms,
                alert.scenario_alert_tier,
                alert.scenario_alert_block_reason,
                alert.sms_allowed_by_stock,
                alert.sms_allowed_by_options,
                alert.sms_block_reason,
                alert.scenario_sms_allowed,
                alert.scenario_sms_block_reason,
                alert.stock_setup_score_reason,
                alert.phase3_heads_up_eligible,
                alert.phase3_heads_up_sent,
                alert.phase3_heads_up_block_reason,
                alert.phase3_heads_up_type,
                alert.phase3_heads_up_dedupe_key,
                alert.phase3_heads_up_dedupe_blocked,
                alert.phase3_heads_up_last_sent_time,
                alert.phase3_heads_up_next_eligible_time,
                alert.market_confirmation_status,
                " | ".join(alert.context_symbols_available),
                " | ".join(alert.strategy_reasons),
                " | ".join(alert.strategy_warnings),
                json.dumps(alert.strategy_levels, sort_keys=True),
                " | ".join(alert.notes),
            ])
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                **asdict(alert),
                "timestamp": alert.timestamp.isoformat(),
            }) + "\n")
        self._append_jsonl(
            self.scenario_jsonl_path,
            {
                "timestamp": alert.timestamp.isoformat(),
                "symbol": alert.symbol,
                "price": alert.price,
                "top_scenario": alert.scenario_top,
                "second_scenario": alert.scenario_second,
                "bullish_score": alert.bullish_score,
                "bearish_score": alert.bearish_score,
                "chop_score": alert.chop_score,
                "fakeout_score": alert.fakeout_score,
                "stage": alert.scenario_stage,
                "score": alert.scenario_score,
                "reasons": alert.scenario_reasons,
                "warnings": alert.scenario_warnings,
                "invalidation_level": (alert.scenario_top or {}).get("invalidation_level") if alert.scenario_top else None,
                "invalidation_reason": (alert.scenario_top or {}).get("invalidation_reason") if alert.scenario_top else None,
                "vwap": alert.scenario_levels.get("vwap") if alert.scenario_levels else None,
                "ema9": alert.scenario_levels.get("ema9") if alert.scenario_levels else None,
                "ema20": alert.scenario_levels.get("ema20") if alert.scenario_levels else None,
                "market_context": alert.market_alignment,
                "alert_tier": alert.alert_tier,
                "alert_tier_reason": alert.alert_tier_reason,
                "alert_source": alert.alert_source,
                "message_source_path": alert.message_source_path,
                "invalidation_level": alert.invalidation_level,
                "invalidation_reason": alert.invalidation_reason,
                "stop_logic_description": alert.stop_logic_description,
                "pullback_required": alert.pullback_required,
                "do_not_chase_warning": alert.do_not_chase_warning,
                "entry_timing_label": alert.entry_timing_label,
                "professional_setup": alert.professional_setup,
                "setup_name": alert.setup_name,
                "setup_code": alert.setup_code,
                "setup_direction": alert.setup_direction,
                "setup_stage": alert.setup_stage,
                "setup_score": alert.setup_score,
                "setup_confidence": alert.setup_confidence,
                "setup_reason": alert.setup_reason,
                "setup_invalidation_level": alert.setup_invalidation_level,
                "setup_entry_quality": alert.setup_entry_quality,
                "setup_risk_label": alert.setup_risk_label,
                "setup_watch_text": alert.setup_watch_text,
                "setup_block_reason": alert.setup_block_reason,
                "scenario_alert_tier": alert.scenario_alert_tier,
                "scenario_alert_eligible": alert.scenario_alert_eligible,
                "scenario_would_sms": alert.scenario_would_sms,
                "scenario_alert_block_reason": alert.scenario_alert_block_reason,
                "scenario_sms_block_reason": alert.scenario_sms_block_reason,
                "stock_setup_score": alert.stock_setup_score,
                "stock_setup_score_reason": alert.stock_setup_score_reason,
                "phase3_heads_up_eligible": alert.phase3_heads_up_eligible,
                "phase3_heads_up_sent": alert.phase3_heads_up_sent,
                "phase3_heads_up_block_reason": alert.phase3_heads_up_block_reason,
                "phase3_heads_up_type": alert.phase3_heads_up_type,
                "phase3_heads_up_dedupe_key": alert.phase3_heads_up_dedupe_key,
                "phase3_heads_up_message_fingerprint": alert.phase3_heads_up_message_fingerprint,
                "phase3_heads_up_dedupe_blocked": alert.phase3_heads_up_dedupe_blocked,
                "phase3_heads_up_dedupe_reason": alert.phase3_heads_up_dedupe_reason,
                "phase3_heads_up_last_sent_time": alert.phase3_heads_up_last_sent_time,
                "phase3_heads_up_next_eligible_time": alert.phase3_heads_up_next_eligible_time,
                "market_confirmation_status": alert.market_confirmation_status,
                "context_symbols_expected": alert.context_symbols_expected,
                "context_symbols_available": alert.context_symbols_available,
                "stock_only_heads_up_allowed": alert.stock_only_heads_up_allowed,
                "stock_only_heads_up_reason": alert.stock_only_heads_up_reason,
                "phase3_heads_up_final_decision": alert.phase3_heads_up_final_decision,
                "phase3_heads_up_final_block_reason": alert.phase3_heads_up_final_block_reason,
                "market_context_missing_warning": alert.market_context_missing_warning,
                "option_stale_did_not_block_heads_up": alert.option_stale_did_not_block_heads_up,
                "watch_only_late_move": alert.watch_only_late_move,
                "do_not_chase_watch": alert.do_not_chase_watch,
                "mixed_signal_detected": alert.mixed_signal_detected,
                "primary_setup_direction": alert.primary_setup_direction,
                "phase3_scenario_direction": alert.phase3_scenario_direction,
                "mixed_signal_reason": alert.mixed_signal_reason,
                "mixed_signal_no_trade": alert.mixed_signal_no_trade,
                "no_trade_reason": alert.no_trade_reason,
                "phone_conclusion": alert.phone_conclusion,
                "phone_conclusion_reason": alert.phone_conclusion_reason,
                "plain_english_conclusion": alert.plain_english_conclusion,
                "alert_decision_label": alert.alert_decision_label,
                "alert_decision_explanation": alert.alert_decision_explanation,
                "decision_tier": alert.decision_tier,
                "decision_label": alert.decision_label,
                "decision_reason": alert.decision_reason,
                "internal_risk_warning_reason": alert.internal_risk_warning_reason,
                "risk_warning_is_actual_risk": alert.risk_warning_is_actual_risk,
                "chop_mode_active": alert.chop_mode_active,
                "chop_mode_type": alert.chop_mode_type,
                "chop_mode_reason": alert.chop_mode_reason,
                "suppressed_by_chop": alert.suppressed_by_chop,
                "missed_clean_entry": alert.missed_clean_entry,
                "bearish_confirmation_quality": alert.bearish_confirmation_quality,
                "bearish_downgraded_by_structure": alert.bearish_downgraded_by_structure,
                "bearish_downgrade_reason": alert.bearish_downgrade_reason,
                "telegram_message_version": alert.telegram_message_version,
                "old_format_removed": alert.old_format_removed,
                "conflict_warning_added": alert.conflict_warning_added,
                "news_context_present": alert.news_context_present,
                "news_used_for_context_only": alert.news_used_for_context_only,
                "news_upgraded_alert": alert.news_upgraded_alert,
            },
        )
        self._append_jsonl(
            self.option_jsonl_path,
            {
                "timestamp": alert.timestamp.isoformat(),
                "timestamp_utc": alert.timestamp.astimezone(UTC).isoformat(),
                **scanner_identity(),
                "symbol": alert.symbol,
                "underlying_symbol": alert.symbol,
                "selected_option_symbol": alert.option_contract,
                "underlying_price": alert.price,
                "strike": alert.option_strike,
                "expiration": alert.option_expiration,
                "option_type": alert.option_type,
                "bid": alert.option_bid,
                "ask": alert.option_ask,
                "mid": alert.option_mid,
                "spread": alert.option_spread_pct,
                "quote_timestamp_raw": alert.option_quote_timestamp_raw,
                "quote_timestamp_utc": alert.option_quote_timestamp_utc,
                "timestamp_source_field": alert.option_timestamp_source_field,
                "timestamp_extraction_failed": alert.option_timestamp_extraction_failed,
                "timestamp_available_fields": alert.option_timestamp_available_fields,
                "scanner_timestamp_utc": alert.timestamp.astimezone(UTC).isoformat(),
                "quote_age_seconds": alert.option_quote_age_seconds,
                "max_allowed_quote_age_seconds": alert.option_max_quote_age_seconds,
                "stale_reason": alert.option_stale_reason,
                "invalid_reason": "missing_bid_or_ask" if normalize_option_quality_label(alert.option_quality) == "INVALID" else "",
                "opra_feed_requested": latest_market_data_status().get("options_feed_requested"),
                "opra_status": latest_market_data_status().get("opra_status"),
                "data_source": alert.option_data_source,
                "fallback_used": alert.option_fallback_used,
                "fallback_type": alert.option_timestamp_fallback_type,
                "fallback_timestamp_utc": alert.option_fallback_timestamp_utc,
                "option_quality_label": alert.option_quality,
                "option_quality_score": alert.options_score,
                "option_quality_message": alert.option_quality_message,
                "option_quality_reasons": alert.option_quality_reasons,
                "days_to_expiration": alert.option_days_to_expiration,
                "is_0dte": alert.option_is_0dte,
                "strike_distance_pct": alert.option_strike_distance_pct,
                "liquidity_state": alert.option_liquidity_state,
                "time_state": alert.option_time_state,
                "stock_only_allowed": alert.option_stock_only_allowed,
                "market_session_status": "active" if options_session_active(alert.timestamp) else "session_closed_or_inactive",
                "alert_score": alert.alert_score,
                "option_feed_status": alert.option_feed_status,
                "option_tradability_score": alert.option_tradability_score,
                "option_warning": alert.sms_block_reason,
                "stock_setup_valid": alert.stock_setup_valid,
                "option_tradable": alert.option_tradable,
                "scenario_alert_eligible": alert.scenario_alert_eligible,
                "scenario_would_sms": alert.scenario_would_sms,
                "dashboard_allowed": True,
                "sms_allowed_by_stock": alert.sms_allowed_by_stock,
                "sms_allowed_by_options": alert.sms_allowed_by_options,
                "final_sms_allowed": alert.sms_allowed,
                "sms_block_reason": alert.sms_block_reason,
                "stock_setup_score_reason": alert.stock_setup_score_reason,
            },
        )
        self._append_jsonl(
            self.news_context_jsonl_path,
            {
                "timestamp": alert.timestamp.isoformat(),
                "symbol": alert.symbol,
                "news_context_present": alert.news_context_present,
                "latest_headline": alert.latest_headline,
                "news_source": alert.news_source,
                "news_age_minutes": alert.news_age_minutes,
                "news_sentiment_guess": alert.news_sentiment_guess,
                "news_used_for_context_only": alert.news_used_for_context_only,
                "news_upgraded_alert": alert.news_upgraded_alert,
                "risk_label": alert.risk_label,
                "option_quality": alert.option_quality,
                "sms_allowed": alert.sms_allowed,
            },
        )
        self._append_jsonl(
            CHOP_MODE_LOG,
            {
                "timestamp": alert.timestamp.isoformat(),
                "symbol": alert.symbol,
                "chop_mode_active": alert.chop_mode_active,
                "chop_mode_type": alert.chop_mode_type,
                "chop_mode_reason": alert.chop_mode_reason,
                "range_low": alert.market_structure_range_low,
                "range_high": alert.market_structure_range_high,
                "suppression_active": alert.chop_suppression_active,
                "suppressed_alert_type": alert.phone_conclusion if alert.suppressed_by_chop else None,
                "suppressed_setup": alert.setup_name if alert.suppressed_by_chop else None,
                "exit_condition_met": not alert.chop_mode_active,
                "market_structure_summary": alert.market_structure_summary,
                "sweep_risk_active": alert.sweep_risk_active,
                "upside_sweep_zone": alert.upside_sweep_zone,
                "downside_sweep_zone": alert.downside_sweep_zone,
                "recent_sweep_count": alert.recent_sweep_count,
                "sweep_risk_reason": alert.sweep_risk_reason,
                "git_commit": git_value("rev-parse", "--short", "HEAD"),
            },
        )
        if alert.missed_clean_entry:
            self._append_jsonl(
                MISSED_CLEAN_ENTRY_LOG,
                {
                    "timestamp": alert.timestamp.isoformat(),
                    "symbol": alert.symbol,
                    "setup_name": alert.setup_name or alert.primary_setup,
                    "direction": alert.scenario_direction or alert.direction,
                    "previous_clean_setup_time": alert.previous_clean_setup_time,
                    "previous_clean_setup_score": alert.previous_clean_setup_score,
                    "current_stage": alert.scenario_stage or alert.setup_stage,
                    "current_price": alert.price,
                    "reason": alert.missed_clean_entry_reason,
                    "lesson": alert.lesson,
                    "git_commit": git_value("rev-parse", "--short", "HEAD"),
                },
            )
        self._append_jsonl(
            self.phase3_heads_up_jsonl_path,
            {
                "timestamp": alert.timestamp.isoformat(),
                "symbol": alert.symbol,
                "direction": alert.scenario_direction or alert.direction,
                "alert_tier": alert.alert_tier,
                "alert_tier_reason": alert.alert_tier_reason,
                "alert_source": alert.alert_source,
                "message_source_path": alert.message_source_path,
                "invalidation_level": alert.invalidation_level,
                "invalidation_reason": alert.invalidation_reason,
                "stop_logic_description": alert.stop_logic_description,
                "pullback_required": alert.pullback_required,
                "do_not_chase_warning": alert.do_not_chase_warning,
                "entry_timing_label": alert.entry_timing_label,
                "professional_setup": alert.professional_setup,
                "setup_name": alert.setup_name,
                "setup_code": alert.setup_code,
                "setup_direction": alert.setup_direction,
                "setup_stage": alert.setup_stage,
                "setup_score": alert.setup_score,
                "setup_confidence": alert.setup_confidence,
                "setup_reason": alert.setup_reason,
                "setup_invalidation_level": alert.setup_invalidation_level,
                "setup_entry_quality": alert.setup_entry_quality,
                "setup_risk_label": alert.setup_risk_label,
                "setup_watch_text": alert.setup_watch_text,
                "setup_block_reason": alert.setup_block_reason,
                "top_scenario": alert.scenario_top,
                "scenario_stage": alert.scenario_stage,
                "scenario_score": alert.scenario_score,
                "stock_setup_score": alert.stock_setup_score,
                "confirmation_score": alert.confirmation_score,
                "risk_label": alert.risk_label,
                "entry_quality_label": alert.entry_quality_label,
                "extension_label": alert.extension_label,
                "option_feed_status": alert.option_feed_status,
                "heads_up_type": alert.phase3_heads_up_type,
                "phase3_heads_up_eligible": alert.phase3_heads_up_eligible,
                "phase3_heads_up_sent": alert.phase3_heads_up_sent,
                "phase3_heads_up_block_reason": alert.phase3_heads_up_block_reason,
                "dedupe_key": alert.phase3_heads_up_dedupe_key,
                "message_fingerprint": alert.phase3_heads_up_message_fingerprint,
                "dedupe_blocked": alert.phase3_heads_up_dedupe_blocked,
                "dedupe_reason": alert.phase3_heads_up_dedupe_reason,
                "last_sent_at": alert.phase3_heads_up_last_sent_time,
                "last_sent_time": alert.phase3_heads_up_last_sent_time,
                "next_eligible_time": alert.phase3_heads_up_next_eligible_time,
                "dedupe_minutes_remaining": alert.phase3_heads_up_dedupe_minutes_remaining,
                "market_confirmation_status": alert.market_confirmation_status,
                "context_symbols_expected": alert.context_symbols_expected,
                "context_symbols_available": alert.context_symbols_available,
                "stock_only_heads_up_allowed": alert.stock_only_heads_up_allowed,
                "stock_only_heads_up_reason": alert.stock_only_heads_up_reason,
                "phase3_heads_up_final_decision": alert.phase3_heads_up_final_decision,
                "phase3_heads_up_final_block_reason": alert.phase3_heads_up_final_block_reason,
                "market_context_missing_warning": alert.market_context_missing_warning,
                "option_stale_did_not_block_heads_up": alert.option_stale_did_not_block_heads_up,
                "watch_only_late_move": alert.watch_only_late_move,
                "do_not_chase_watch": alert.do_not_chase_watch,
                "telegram_attempted": bool(alert.phase3_heads_up_sent),
                "telegram_sent": False,
                "telegram_error": "",
                "message_preview": alert.phase3_heads_up_message_preview,
                "scenario_reasons": alert.scenario_reasons,
                "scenario_warnings": alert.scenario_warnings,
            },
        )
        self._append_jsonl(
            self.market_regime_jsonl_path,
            {
                "timestamp": alert.timestamp.isoformat(),
                "symbol": alert.symbol,
                "alert_symbol": alert.symbol == "AAPL",
                "context_symbols": ["SPY", "QQQ"],
                "market_regime": alert.market_regime,
                "regime_score": alert.regime_score if alert.regime_score is not None else alert.market_score,
                "regime_reason": alert.regime_reason,
                "spy_alignment": alert.spy_alignment,
                "qqq_alignment": alert.qqq_alignment,
                "aapl_relative_strength": alert.aapl_relative_strength,
                "volume_state": alert.volume_state,
                "volatility_state": alert.volatility_state,
                "alert_tier": alert.alert_tier,
                "sms_allowed": alert.sms_allowed,
                "watch_allowed": alert.watch_allowed,
            },
        )
        self._append_jsonl(
            self.multi_timeframe_jsonl_path,
            {
                "timestamp": alert.timestamp.isoformat(),
                "symbol": alert.symbol,
                "alert_symbol": alert.symbol == "AAPL",
                "context_symbols": ["SPY", "QQQ"],
                "trend_1m": alert.trend_1m,
                "trend_5m": alert.trend_5m,
                "trend_15m": alert.trend_15m,
                "daily_trend": alert.daily_trend,
                "current_bias": alert.current_structure_bias,
                "key_warning": alert.structure_key_warning,
                "nearest_level_name": alert.nearest_level_name,
                "nearest_level_price": alert.nearest_level_price,
                "distance_to_key_level_pct": alert.distance_to_key_level_pct,
                "nearest_support": alert.nearest_support,
                "nearest_resistance": alert.nearest_resistance,
                "levels": alert.multi_timeframe_levels,
                "demand_zones": alert.demand_zones,
                "supply_zones": alert.supply_zones,
                "liquidity_above_highs": alert.liquidity_above_highs,
                "liquidity_below_lows": alert.liquidity_below_lows,
            },
        )

    def _append_jsonl(self, path: Path, payload: Dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")


# ------------------------------------------------------------
# Discord notifier
# ------------------------------------------------------------
PROFESSIONAL_ALERT_TIERS = {
    "CONTEXT",
    "SETUP_FORMING",
    "SETUP_CONFIRMED",
    "TRADE_QUALITY_WATCH",
    "RISK_WARNING",
}


def _valid_level(value: Any) -> Optional[float]:
    try:
        level = float(value)
    except (TypeError, ValueError):
        return None
    return level if level > 0 else None


def apply_risk_invalidation(alert: Alert) -> Alert:
    scenario = alert.scenario_top or {}
    direction = str(alert.scenario_direction or alert.direction or alert.strategy_direction or "").upper()
    stage = str(alert.scenario_stage or scenario.get("stage") or "").upper()
    entry = str(alert.entry_quality_label or alert.scenario_entry_quality_label or "").upper()
    extension = str(alert.extension_label or "").upper()
    risk = str(alert.risk_label or alert.scenario_risk_label or "").upper()

    if risk == "DO_NOT_CHASE" or stage == "DO_NOT_CHASE" or entry == "DO_NOT_CHASE" or extension == "DO_NOT_CHASE":
        timing = "DO_NOT_CHASE"
    elif stage == "LATE" or entry == "LATE" or extension in {"EXTENDED", "VERY_EXTENDED"}:
        timing = "LATE"
    elif stage == "GOOD_POSITION" or entry == "GOOD_POSITION":
        timing = "GOOD_POSITION"
    else:
        timing = "EARLY"
    alert.entry_timing_label = timing
    alert.pullback_required = timing in {"LATE", "DO_NOT_CHASE"}
    alert.do_not_chase_warning = timing == "DO_NOT_CHASE" or risk == "DO_NOT_CHASE"

    scenario_level = _valid_level(scenario.get("invalidation_level"))
    scenario_reason = str(scenario.get("invalidation_reason") or "").strip()
    if scenario_level is not None:
        alert.invalidation_level = scenario_level
        alert.invalidation_reason = scenario_reason or (
            "Bullish setup fails below this support level"
            if direction == "BULLISH"
            else "Bearish setup fails above this resistance level"
        )
    else:
        levels = {**(alert.strategy_levels or {}), **(alert.scenario_levels or {})}
        if alert.trigger_level is not None:
            levels.setdefault("trigger_level", alert.trigger_level)
        level_candidates: List[Tuple[float, str]] = []
        reason_by_level_bullish = {
            "vwap": "Bullish setup invalidates if price loses VWAP",
            "ema9": "Bullish setup invalidates if price loses EMA9",
            "ema20": "Bullish setup invalidates if price loses EMA20",
            "recent_low": "Bullish setup invalidates below the recent swing low",
            "recent_swing_low": "Bullish setup invalidates below the recent swing low",
            "swept_level": "Bullish reclaim fails if price loses the swept level",
            "opening_range_low": "Bullish setup invalidates below opening range support",
            "pml": "Bullish setup invalidates below premarket low",
            "pdl": "Bullish setup invalidates below previous-day low",
            "trigger_level": "Bullish setup invalidates if the reclaimed trigger level is lost",
        }
        reason_by_level_bearish = {
            "vwap": "Bearish setup invalidates if price reclaims VWAP",
            "ema9": "Bearish setup invalidates if price reclaims EMA9",
            "ema20": "Bearish setup invalidates if price reclaims EMA20",
            "recent_high": "Bearish setup invalidates above the recent swing high",
            "recent_swing_high": "Bearish setup invalidates above the recent swing high",
            "swept_level": "Bearish rejection fails above the sweep high",
            "opening_range_high": "Bearish setup invalidates above opening range resistance",
            "pmh": "Bearish setup invalidates above premarket high",
            "pdh": "Bearish setup invalidates above previous-day high",
            "trigger_level": "Bearish setup invalidates if the breakdown trigger level is reclaimed",
        }
        reason_map = reason_by_level_bullish if direction == "BULLISH" else reason_by_level_bearish
        for name, reason in reason_map.items():
            level = _valid_level(levels.get(name))
            if level is None:
                continue
            if direction == "BULLISH" and level <= alert.price:
                level_candidates.append((level, reason))
            elif direction == "BEARISH" and level >= alert.price:
                level_candidates.append((level, reason))
        if level_candidates:
            selected = max(level_candidates, key=lambda item: item[0]) if direction == "BULLISH" else min(level_candidates, key=lambda item: item[0])
            alert.invalidation_level, alert.invalidation_reason = selected

    if alert.invalidation_level is not None and alert.invalidation_reason:
        alert.stop_logic_description = (
            f"Idea is wrong on a confirmed close below {alert.invalidation_level:.2f}; {alert.invalidation_reason}."
            if direction == "BULLISH"
            else f"Idea is wrong on a confirmed close above {alert.invalidation_level:.2f}; {alert.invalidation_reason}."
        )
    else:
        alert.invalidation_level = None
        alert.invalidation_reason = "No clean invalidation level available — watch only until structure improves"
        alert.stop_logic_description = "No clean structural risk level is available; do not treat this as trade-quality."
        if alert.sms_allowed:
            alert.sms_allowed = False
            alert.text_alert_reason = "trade-quality blocked: no clean invalidation level"
        alert.scenario_would_sms = False if alert.scenario_would_sms is not None else None
        alert.scenario_sms_allowed = False if alert.scenario_sms_allowed is not None else None
        alert.scenario_sms_block_reason = alert.scenario_sms_block_reason or "No clean invalidation level available"
        alert.sms_block_reason = alert.sms_block_reason or "No clean invalidation level available"
    if alert.pullback_required and "Wait for pullback/retest before entry." not in alert.strategy_warnings:
        alert.strategy_warnings.append("Wait for pullback/retest before entry.")
    if alert.do_not_chase_warning and "Do Not Chase — setup is extended or late." not in alert.strategy_warnings:
        alert.strategy_warnings.insert(0, "Do Not Chase — setup is extended or late.")
    return alert


def assign_professional_alert_tier(alert: Alert) -> Alert:
    category = str(alert.category or "").upper()
    direction = str(alert.scenario_direction or alert.direction or alert.strategy_direction or "").upper()
    stage = str(alert.scenario_stage or (alert.scenario_top or {}).get("stage") or "").upper()
    risk = str(alert.risk_label or alert.scenario_risk_label or "").upper()
    entry = str(alert.entry_quality_label or alert.scenario_entry_quality_label or "").upper()
    option_quality = str(alert.option_quality or "").upper()
    warning_text = " ".join(
        [*alert.strategy_warnings, *alert.scenario_warnings, alert.text_alert_reason or ""]
    ).upper()
    risk_warning = (
        risk in {"HIGH", "DO_NOT_CHASE"}
        or entry in {"LATE", "DO_NOT_CHASE"}
        or stage in {"LATE", "DO_NOT_CHASE", "INVALIDATED"}
        or alert.market_regime == "CHOPPY"
        or bool(alert.mixed_signal_detected or alert.scenario_conflict)
        or (
            bool(option_quality)
            and normalize_option_quality_label(option_quality) in {
                "WIDE_SPREAD", "POOR_QUALITY", "STALE", "INVALID", "TOO_RISKY_0DTE", "LOW_LIQUIDITY"
            }
        )
        or any(term in warning_text for term in ("DO NOT CHASE", "WIDE SPREAD", "MIXED SIGNAL", "DIRECTION CONFLICT"))
    )
    no_clean_invalidation = (
        alert.invalidation_level is None
        and direction in {"BULLISH", "BEARISH"}
        and "NO CLEAN INVALIDATION" in str(alert.invalidation_reason or "").upper()
    )
    if risk_warning:
        tier = "RISK_WARNING"
        reason = (
            "choppy market regime requires watch-only manual review"
            if alert.market_regime == "CHOPPY"
            else "late/chase/mixed-signal/option-quality risk requires manual review"
        )
    elif no_clean_invalidation:
        tier = "SETUP_FORMING"
        reason = "watch only until a clean invalidation level is available"
    elif alert.sms_allowed and bool(alert.option_tradable or option_quality_is_tradable(alert.option_quality)):
        tier = "TRADE_QUALITY_WATCH"
        reason = "existing strict alert approval passed with a tradable option"
    elif stage in {"CONFIRMED", "GOOD_POSITION"} or alert.phase3_heads_up_type == "GOOD_POSITION":
        tier = "SETUP_CONFIRMED"
        reason = "Phase 3 setup is confirmed or in a good-position stage"
    elif (
        alert.watch_allowed
        or alert.phase3_heads_up_sent
        or stage in {"WATCHING", "FORMING"}
        or bool(alert.primary_setup)
    ):
        tier = "SETUP_FORMING"
        reason = "setup is developing and still requires confirmation"
    else:
        tier = "CONTEXT"
        reason = "context or key-level awareness only"
    if category.startswith("WATCH ") and not alert.primary_setup and stage not in {"CONFIRMED", "GOOD_POSITION"}:
        tier = "CONTEXT"
        reason = "key level is approaching; wait for setup confirmation"
    alert.alert_tier = tier
    alert.alert_tier_reason = reason
    if alert.phase3_heads_up_sent:
        alert.alert_source = "STOCK_ONLY_WARNING" if alert.stock_only_heads_up_allowed else "PHASE3_HEADS_UP"
        alert.message_source_path = "phase3_heads_up_message"
    elif alert.sms_allowed:
        alert.alert_source = "NORMAL_SMS"
        alert.message_source_path = "format_alert_message"
    elif alert.watch_allowed:
        alert.alert_source = "NORMAL_WATCH"
        alert.message_source_path = "format_alert_message"
    else:
        alert.alert_source = "SCANNER_CONTEXT"
        alert.message_source_path = "format_alert_message"
    assign_phone_conclusion(alert)
    return alert


def assign_phone_conclusion(alert: Alert) -> Alert:
    setup = str(alert.setup_name or alert.primary_setup or (alert.scenario_top or {}).get("scenario_name") or "").upper()
    stage = str(alert.setup_stage or alert.scenario_stage or (alert.scenario_top or {}).get("stage") or "").upper()
    entry = str(alert.entry_timing_label or alert.entry_quality_label or "").upper()
    risk = str(alert.setup_risk_label or alert.risk_label or alert.scenario_risk_label or "").upper()
    option_quality = normalize_option_quality_label(alert.option_quality or alert.option_feed_status)
    mixed = bool(alert.mixed_signal_detected or alert.scenario_conflict or setup == "MIXED SIGNAL")
    context_only = (
        alert.alert_tier == "CONTEXT"
        or (
            not setup
            and stage not in {"FORMING", "CONFIRMED", "GOOD_POSITION"}
        )
    )
    specific_risks: List[str] = []
    if str(alert.market_regime or "").upper() in {"CHOPPY", "RANGE_BOUND", "LOW_VOLUME_FAKE_MOVE"}:
        specific_risks.append(f"{str(alert.market_regime).lower().replace('_', '-')} market")
    if option_quality in {"WIDE_SPREAD", "POOR_QUALITY", "STALE", "INVALID", "TOO_RISKY_0DTE", "LOW_LIQUIDITY"}:
        specific_risks.append(f"option {option_quality.lower().replace('_', ' ')}")
    if alert.confirmation_score is not None and alert.confirmation_score < 60:
        specific_risks.append("confirmation below 60")

    if alert.chop_mode_active:
        conclusion = "CHOP MODE"
        reason = alert.chop_mode_reason or "No clean edge inside the active range."
    elif alert.missed_clean_entry:
        conclusion = "DO NOT CHASE"
        reason = alert.missed_clean_entry_reason or "Earlier clean setup is now late."
    elif mixed:
        conclusion = "MIXED / NO TRADE"
        reason = "Signals conflict. Wait for a cleaner setup."
        alert.mixed_signal_detected = True
        alert.mixed_signal_no_trade = True
        alert.no_trade_reason = alert.mixed_signal_reason or reason
    elif stage in {"LATE", "DO_NOT_CHASE"} or entry in {"LATE", "DO_NOT_CHASE"} or risk == "DO_NOT_CHASE":
        conclusion = "DO NOT CHASE"
        reason = "Move is late or extended. Wait for a pullback/retest."
    elif context_only:
        conclusion = "CONTEXT ONLY"
        reason = "No clean setup is confirmed yet."
    elif alert.alert_tier == "TRADE_QUALITY_WATCH" and not mixed:
        conclusion = "TRADE QUALITY WATCH"
        reason = "Confirmed setup, supportive context, tradable option, and a defined invalidation."
    elif alert.alert_tier == "RISK_WARNING" and specific_risks:
        conclusion = "RISK WARNING"
        reason = ", ".join(dict.fromkeys(specific_risks[:3]))
    else:
        conclusion = "WATCH ONLY"
        reason = "Setup is interesting but still needs manual confirmation."

    alert.phone_conclusion = conclusion
    alert.phone_conclusion_reason = reason
    alert.plain_english_conclusion = reason
    alert.alert_decision_label = conclusion
    alert.alert_decision_explanation = reason
    actual_option_risk = option_quality in {
        "WIDE_SPREAD", "POOR_QUALITY", "STALE", "INVALID", "TOO_RISKY_0DTE", "LOW_LIQUIDITY"
    }
    if alert.chop_mode_active:
        alert.decision_tier = "CHOP_MODE"
        alert.decision_label = "CHOP_MODE"
        alert.decision_reason = alert.chop_mode_reason or "No clean edge inside the active range"
    elif alert.missed_clean_entry:
        alert.decision_tier = "DO_NOT_CHASE"
        alert.decision_label = "MISSED_CLEAN_ENTRY_NOW_LATE"
        alert.decision_reason = alert.missed_clean_entry_reason
    elif conclusion == "MIXED / NO TRADE":
        alert.decision_tier = "MIXED_NO_TRADE"
        alert.decision_label = "MIXED_NO_TRADE"
        alert.decision_reason = reason
    elif conclusion == "DO NOT CHASE":
        alert.decision_tier = "DO_NOT_CHASE"
        alert.decision_label = "DO_NOT_CHASE"
        alert.decision_reason = reason
    elif actual_option_risk:
        alert.decision_tier = "RISK_WARNING"
        alert.decision_label = "RISK_WARNING"
        alert.decision_reason = option_quality_message(option_quality)
    elif conclusion == "TRADE QUALITY WATCH":
        alert.decision_tier = "TRADE_QUALITY_WATCH"
        alert.decision_label = "TRADE_QUALITY_WATCH"
        alert.decision_reason = reason
    elif conclusion == "WATCH ONLY":
        alert.decision_tier = "WATCH_ONLY"
        alert.decision_label = "WATCH_ONLY"
        alert.decision_reason = reason
    else:
        alert.decision_tier = "CONTEXT_ONLY"
        alert.decision_label = "CONTEXT_ONLY"
        alert.decision_reason = reason
    alert.risk_warning_is_actual_risk = actual_option_risk
    alert.internal_risk_warning_reason = option_quality_message(option_quality) if actual_option_risk else None
    alert.telegram_message_version = "CONCLUSION_FIRST_V2"
    alert.old_format_removed = True
    return alert


def confirmation_required_for_tier(alert: Alert) -> str:
    if alert.alert_tier == "TRADE_QUALITY_WATCH":
        return "Existing strict approval passed; confirm timing and risk manually."
    if alert.alert_tier == "SETUP_CONFIRMED":
        return "Confirm hold/retest and chart alignment."
    if alert.alert_tier == "SETUP_FORMING":
        return "Wait for confirmation before acting."
    if alert.alert_tier == "RISK_WARNING":
        return "Do not chase; review risk and wait for a cleaner setup."
    return "Context only; wait for a valid setup."


def telegram_risk_warning_reason(alert: Alert) -> Optional[str]:
    if alert.alert_tier != "RISK_WARNING":
        return None

    reasons: List[str] = []
    regime = str(alert.market_regime or "").upper()
    risk = str(alert.setup_risk_label or alert.risk_label or alert.scenario_risk_label or "").upper()
    entry = str(alert.entry_timing_label or alert.entry_quality_label or "").upper()
    option_quality = normalize_option_quality_label(alert.option_quality or alert.option_feed_status)
    candle = str(alert.candle_label or "").upper()

    if regime in {"CHOPPY", "RANGE_BOUND"}:
        reasons.append(f"{regime.lower().replace('_', '-')} market")
    if risk in {"HIGH", "DO_NOT_CHASE"}:
        reasons.append(f"setup risk {risk}")
    if entry in {"LATE", "DO_NOT_CHASE"}:
        reasons.append(f"entry timing {entry}")
    if option_quality in {"WIDE_SPREAD", "POOR_QUALITY", "STALE", "INVALID", "TOO_RISKY_0DTE", "LOW_LIQUIDITY"}:
        reasons.append(f"option {option_quality.lower().replace('_', ' ')}")
    if alert.confirmation_score is not None and alert.confirmation_score < 60:
        reasons.append("confirmation below 60")
    if alert.mixed_signal_detected or alert.scenario_conflict:
        reasons.append("mixed/direction conflict")
    if candle in {"INDECISION", "REJECTION"}:
        reasons.append(f"candle {candle.lower()}")
    if not reasons:
        reasons.append(alert.alert_tier_reason or "manual risk review required")
    return ", ".join(dict.fromkeys(reasons[:4]))


def _telegram_title(symbol: str, alert_type: str) -> str:
    titles = {
        "PHASE3_HEADS_UP": "Phase 3 Heads-Up",
        "NORMAL_SMS": "Scanner Alert",
        "NORMAL_WATCH": "Watch Alert",
        "STOCK_ONLY_WARNING": "Watch-Only Warning",
    }
    return f"{symbol} {titles.get(str(alert_type).upper(), 'Scanner Alert')}"


def _telegram_reason(alert: Alert, scenario: Dict[str, Any]) -> str:
    if alert.chop_mode_active:
        return alert.chop_mode_reason or "AAPL is trading without a clean edge."
    if alert.missed_clean_entry:
        return alert.missed_clean_entry_reason or "Earlier clean setup is now late."
    if alert.mixed_signal_no_trade:
        return alert.phone_conclusion_reason or "Signals conflict. Wait for a cleaner setup."
    if alert.setup_reason:
        return alert.setup_reason
    reasons = scenario.get("reasons") or alert.scenario_reasons or alert.strategy_reasons
    if reasons:
        return "; ".join(str(reason) for reason in reasons[:2])
    return "No clear setup reason"


def _short_phone_text(value: Any, limit: int = 150) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compact_alert_message(alert: Alert) -> str:
    apply_risk_invalidation(alert)
    assign_professional_alert_tier(alert)
    level = "WATCH" if alert.setup_level == "WATCH" else "ALERT"
    direction = alert.direction or "MOMENTUM"
    detected = alert.timestamp.astimezone(ET).strftime("%-I:%M %p ET")
    parts = [
        f"Tier {alert.alert_tier}",
        f"{level} {alert.symbol} {direction}",
        f"${alert.price:.2f}",
        f"at {detected}",
    ]
    if alert.trigger_level is not None:
        parts.append(f"level {alert.trigger_level:.2f}")
    elif "OPENING RANGE" in alert.category and alert.opening_range_high is not None and direction == "BULLISH":
        parts.append(f"OR high {alert.opening_range_high:.2f}")
    elif "OPENING RANGE" in alert.category and alert.opening_range_low is not None and direction == "BEARISH":
        parts.append(f"OR low {alert.opening_range_low:.2f}")
    if alert.fast_move_pct is not None:
        parts.append(f"fast {alert.fast_move_pct:+.2f}%")
    if alert.day_move_pct is not None:
        parts.append(f"day {alert.day_move_pct:+.2f}%")
    if alert.relative_volume is not None:
        parts.append(f"RVOL {alert.relative_volume:.2f}x")
    if alert.primary_setup:
        parts.append(f"setup {alert.primary_setup}")
    if alert.mixed_signal_detected:
        parts.append("MIXED SIGNAL")
    if alert.scenario_top and (alert.scenario_top.get("scenario_name") or alert.scenario_stage):
        scenario_name = alert.scenario_top.get("scenario_name") or "scenario"
        scenario_stage = alert.scenario_stage or alert.scenario_top.get("stage") or ""
        parts.append(f"scenario {scenario_name} {scenario_stage}".strip())
    if alert.strategy_confidence_score is not None:
        parts.append(f"conf {alert.strategy_confidence_score} {alert.strategy_confidence_label or ''}".strip())
    if alert.confirmation_score is not None:
        parts.append(f"confirm {alert.confirmation_score} {alert.confirmation_label or ''}".strip())
    if alert.entry_quality_label and alert.entry_quality_label != "UNKNOWN":
        parts.append(f"entry {alert.entry_quality_label}")
    if alert.volume_label:
        parts.append(f"vol {alert.volume_label}")
    if alert.candle_label:
        parts.append(f"candle {alert.candle_label}")
    if alert.extension_label and alert.extension_label not in {"NORMAL", "UNKNOWN"}:
        parts.append(f"ext {alert.extension_label}")
    if alert.relative_strength_label and alert.relative_strength_label not in {"NEUTRAL", "UNKNOWN"}:
        parts.append(f"RS {alert.relative_strength_label}")
    if alert.market_regime and alert.market_regime != "UNKNOWN":
        parts.append(f"market {alert.market_regime}")
    if alert.pressure_label and alert.pressure_label != "UNKNOWN":
        parts.append(f"pressure {alert.pressure_label}")
    if alert.option_feed_status and alert.option_feed_status != "UNAVAILABLE":
        parts.append(f"feed {alert.option_feed_status}")
    if alert.news_context_present:
        parts.append("news context only")
    if alert.risk_label == "DO_NOT_CHASE":
        parts.append("RISK: DO_NOT_CHASE — valid setup may be late")
    elif alert.risk_label:
        parts.append(f"risk {alert.risk_label}")
    if alert.strategy_warnings:
        parts.append(alert.strategy_warnings[0])
    if alert.option_quality:
        opt = alert.option_quality
        if alert.option_spread_pct is not None:
            opt += f" {alert.option_spread_pct:.1f}%spr"
        parts.append(f"opt {opt}")
    if alert.alert_grade:
        parts.append(f"grade {alert.alert_grade}")
    parts.append("confirm in Webull")
    return " | ".join(parts)


def phase3_heads_up_message(alert: Alert) -> str:
    scenario = alert.scenario_top or {}
    scenario_name = scenario.get("scenario_name") or alert.primary_setup or "Scenario"
    stage = alert.scenario_stage or scenario.get("stage") or ""
    direction = (alert.scenario_direction or scenario.get("direction") or alert.direction or "").upper()
    direction_text = "Bullish" if direction == "BULLISH" else "Bearish" if direction == "BEARISH" else "Directional"
    reasons = list(alert.scenario_reasons or alert.strategy_reasons or [])
    reason_text = ", ".join(reasons[:3]) if reasons else "Strong Phase 3 scenario read."
    early = alert.phase3_heads_up_type == "EARLY_WATCH"
    stock_only = alert.phase3_heads_up_type == "STOCK_ONLY_WARNING"
    late_move = alert.phase3_heads_up_type == "WATCH_ONLY_LATE_MOVE"
    do_not_chase_watch = alert.phase3_heads_up_type == "DO_NOT_CHASE_WATCH"
    if do_not_chase_watch:
        return "\n".join(
            [
                f"{alert.symbol} Watch-Only Warning",
                "Do Not Chase",
                "Price is extended.",
                "Wait for pullback/retest.",
                "Not a buy/sell signal.",
                "Confirm manually on chart.",
            ]
        )
    if late_move:
        invalidation = scenario.get("invalidation_reason")
        if not invalidation and scenario.get("invalidation_level") is not None:
            invalidation = f"loses/recovers {float(scenario['invalidation_level']):.2f}"
        lines = [
            f"{alert.symbol} Phase 3 Watch-Only Heads-Up",
            f"{direction_text} {scenario_name} — WATCH ONLY — LATE / DO NOT CHASE",
            "This is not trade-ready.",
            "This is not a buy/sell signal.",
            "Do not chase. Wait for pullback/retest.",
            f"Score {alert.scenario_score or 0} | Stock {alert.stock_setup_score or 0} | Confirm {alert.confirmation_score or 0}",
            f"Reason: {reason_text}",
        ]
        if invalidation:
            lines.append(f"Invalidation: {invalidation}.")
        if alert.option_stale_did_not_block_heads_up:
            lines.append("Warning: Option quote stale/missing — stock setup only.")
        if alert.mixed_signal_detected and alert.mixed_signal_reason:
            lines.append(f"Warning: Mixed signal / conflict — {alert.mixed_signal_reason}")
        lines.append("Confirm manually on chart.")
        return "\n".join(lines)
    title = "Phase 3 Stock Heads-Up" if stock_only else "Phase 3 Early Heads-Up" if early else "Phase 3 Heads-Up"
    display_stage = f"{stage} WARNING" if stock_only and stage in {"LATE", "DO_NOT_CHASE"} else stage
    reminder = (
        "Heads-up only — not a buy/sell signal."
        if stock_only
        else "Early heads-up only — watch chart, do not enter yet."
        if early
        else "Possible setup — confirm manually on chart."
    )
    watch_text = (
        "next candle hold / break above recent high"
        if direction == "BULLISH"
        else "next candle rejection / break below recent low"
    )
    invalidation = scenario.get("invalidation_reason")
    if not invalidation and scenario.get("invalidation_level") is not None:
        invalidation = f"loses/recovers {float(scenario['invalidation_level']):.2f}"
    warnings = ["Heads-up only — confirm on chart."]
    if any("direction conflict" in warning.lower() for warning in alert.strategy_warnings + alert.scenario_warnings):
        warnings.append("Legacy/Phase 2 conflict present — confirm manually.")
    lines = [
        f"{alert.symbol} {title}",
        f"{direction_text} {scenario_name} — {display_stage}".strip(),
        reminder,
        f"Score {alert.scenario_score or 0} | Stock {alert.stock_setup_score or 0} | Confirm {alert.confirmation_score or 0}",
        f"Reason: {reason_text}",
        f"Watch: {watch_text}.",
    ]
    if invalidation:
        lines.append(f"Invalidation: {invalidation}.")
    if stock_only:
        warning_text = " ".join(alert.scenario_warnings[-5:] or warnings)
        lines.append(f"Warning: {warning_text}")
        lines.append("Heads-up only — not a buy/sell signal.")
    else:
        lines.append(f"Reminder: {' '.join(warnings)}")
    return "\n".join(lines)


def phase3_heads_up_dedupe_key(alert: Alert) -> str:
    scenario = alert.scenario_top or {}
    scenario_name = str(scenario.get("scenario_name") or alert.primary_setup or "SCENARIO").strip()
    direction = str(alert.scenario_direction or scenario.get("direction") or alert.direction or "MOMENTUM").upper()
    stage = str(alert.scenario_stage or scenario.get("stage") or "UNKNOWN").upper()
    alert_type = str(alert.phase3_heads_up_type or "PHASE3_HEADS_UP").upper()
    return f"{alert.symbol.upper()}|{scenario_name}|{stage}|{direction}|{alert_type}"


def phase3_heads_up_message_fingerprint(alert: Alert) -> str:
    return hashlib.sha256(phase3_heads_up_message(alert).encode("utf-8")).hexdigest()


def phase3_heads_up_record(alert: Alert, sent_at: Optional[datetime] = None) -> Dict[str, Any]:
    scenario = alert.scenario_top or {}
    return {
        "sent_at": (sent_at or now_utc()).astimezone(UTC).isoformat(),
        "dedupe_key": phase3_heads_up_dedupe_key(alert),
        "message_fingerprint": phase3_heads_up_message_fingerprint(alert),
        "symbol": alert.symbol.upper(),
        "scenario": str(scenario.get("scenario_name") or alert.primary_setup or "SCENARIO").strip(),
        "stage": str(alert.scenario_stage or scenario.get("stage") or "UNKNOWN").upper(),
        "direction": str(alert.scenario_direction or scenario.get("direction") or alert.direction or "MOMENTUM").upper(),
        "scenario_score": int(alert.scenario_score or scenario.get("score") or 0),
        "key_level": (scenario.get("invalidation_level") if scenario else None),
    }


def phase3_heads_up_meaningful_change(previous: Dict[str, Any], current: Dict[str, Any]) -> Optional[str]:
    if previous.get("scenario") != current.get("scenario"):
        return "scenario changed"
    if previous.get("direction") != current.get("direction"):
        return "direction changed"
    if previous.get("stage") != current.get("stage"):
        return "scenario stage changed"
    if abs(int(current.get("scenario_score") or 0) - int(previous.get("scenario_score") or 0)) >= 10:
        return "scenario score changed by at least 10 points"
    previous_level = previous.get("key_level")
    current_level = current.get("key_level")
    if previous_level is not None and current_level is not None and previous_level != current_level:
        return "key level changed"
    return None


def phase3_heads_up_dedupe_decision(
    previous: Optional[Dict[str, Any]],
    current: Dict[str, Any],
    dedupe_minutes: int,
    now: Optional[datetime] = None,
) -> Tuple[bool, str, Optional[datetime]]:
    if not previous:
        return True, "first Phase 3 heads-up", None
    try:
        last_sent = datetime.fromisoformat(str(previous.get("sent_at")))
    except (TypeError, ValueError):
        return True, "previous send time unavailable", None
    current_time = now or now_utc()
    if (current_time - last_sent).total_seconds() > dedupe_minutes * 60:
        return True, "Phase 3 heads-up cooldown elapsed", last_sent
    if previous.get("message_fingerprint") == current.get("message_fingerprint"):
        return False, "duplicate blocked: identical Phase 3 heads-up fingerprint within cooldown", last_sent
    meaningful_change = phase3_heads_up_meaningful_change(previous, current)
    if meaningful_change:
        return True, meaningful_change, last_sent
    return False, "duplicate blocked: near-identical Phase 3 heads-up within cooldown", last_sent


def format_alert_message(alert: Alert, markdown: bool = True) -> str:
    apply_risk_invalidation(alert)
    assign_professional_alert_tier(alert)
    if not markdown:
        return compact_alert_message(alert)

    bold = "**" if markdown else ""
    body_lines = [
        f"{bold}{alert.symbol}{bold} @ {bold}${alert.price:.2f}{bold}",
        f"Alert tier: {bold}{alert.alert_tier}{bold}",
        f"Tier reason: {alert.alert_tier_reason}",
        f"Category: {bold}{alert.category}{bold}",
    ]
    if alert.setup_level:
        body_lines.append(f"Level: {bold}{alert.setup_level}{bold}")
    if alert.trigger_level is not None:
        body_lines.append(f"Trigger level: {bold}{alert.trigger_level:.2f}{bold}")
    if alert.alert_grade:
        body_lines.append(f"Grade: {bold}{alert.alert_grade}{bold} ({alert.alert_score or 0}/100)")
    if alert.direction:
        body_lines.append(f"Read: {bold}{alert.direction}{bold}")
    if alert.primary_setup:
        body_lines.append(f"Primary setup: {bold}{alert.primary_setup}{bold}")
    if alert.setup_name:
        body_lines.append(
            f"Official setup: {bold}{alert.setup_name}{bold} | "
            f"{alert.setup_direction or 'neutral'} | {alert.setup_stage or 'WATCHING'} | "
            f"{alert.setup_score or 0} {alert.setup_confidence or 'LOW'}"
        )
        body_lines.append(f"Setup reason: {alert.setup_reason or 'No clear setup reason'}")
        if alert.setup_block_reason:
            body_lines.append(f"Setup block: {alert.setup_block_reason}")
    if alert.scenario_top:
        scenario_name = alert.scenario_top.get("scenario_name", "")
        scenario_stage = alert.scenario_stage or alert.scenario_top.get("stage", "")
        body_lines.append(f"Scenario: {bold}{scenario_name} {scenario_stage}{bold}".strip())
    if alert.secondary_setups:
        body_lines.append(f"Secondary setups: {bold}{', '.join(alert.secondary_setups)}{bold}")
    if alert.strategy_confidence_score is not None:
        body_lines.append(
            f"Strategy confidence: {bold}{alert.strategy_confidence_score} {alert.strategy_confidence_label or ''}{bold}"
        )
    if alert.confirmation_score is not None:
        body_lines.append(
            f"Confirmation: {bold}{alert.confirmation_score} {alert.confirmation_label or ''}{bold}"
        )
    if alert.entry_quality_label and alert.entry_quality_label != "UNKNOWN":
        body_lines.append(f"Entry quality: {bold}{alert.entry_quality_label}{bold}")
    if alert.volume_label:
        rvol_text = f" RVOL {alert.rvol_detail:.2f}x" if alert.rvol_detail is not None else ""
        body_lines.append(f"Volume quality: {bold}{alert.volume_label}{rvol_text}{bold}")
    if alert.candle_label:
        score_text = f" {alert.candle_score}" if alert.candle_score is not None else ""
        body_lines.append(f"Candle quality: {bold}{alert.candle_label}{score_text}{bold}")
    if alert.extension_label and alert.extension_label not in {"NORMAL", "UNKNOWN"}:
        score_text = f" {alert.extension_score}" if alert.extension_score is not None else ""
        body_lines.append(f"Extension: {bold}{alert.extension_label}{score_text}{bold}")
    if alert.relative_strength_label and alert.relative_strength_label not in {"NEUTRAL", "UNKNOWN"}:
        score_text = f" {alert.relative_strength_score}" if alert.relative_strength_score is not None else ""
        body_lines.append(f"Relative strength: {bold}{alert.relative_strength_label}{score_text}{bold}")
    if alert.market_regime and alert.market_regime != "UNKNOWN":
        score_text = f" {alert.regime_score if alert.regime_score is not None else alert.market_score}" if alert.regime_score is not None or alert.market_score is not None else ""
        body_lines.append(f"Market regime: {bold}{alert.market_regime}{score_text}{bold}")
        if alert.regime_reason:
            body_lines.append(f"Market environment: {alert.regime_reason}")
        body_lines.append(
            f"Market alignment: SPY {bold}{alert.spy_alignment or 'UNKNOWN'}{bold} | "
            f"QQQ {bold}{alert.qqq_alignment or 'UNKNOWN'}{bold} | "
            f"AAPL relative strength {bold}{alert.aapl_relative_strength or 'UNKNOWN'}{bold}"
        )
        body_lines.append(
            f"Market state: volume {bold}{alert.volume_state or 'UNKNOWN'}{bold} | "
            f"volatility {bold}{alert.volatility_state or 'UNKNOWN'}{bold}"
        )
    if alert.current_structure_bias:
        body_lines.append(
            f"Structure: 1m {bold}{alert.trend_1m or 'UNKNOWN'}{bold} | "
            f"5m {bold}{alert.trend_5m or 'UNKNOWN'}{bold} | "
            f"15m {bold}{alert.trend_15m or 'UNKNOWN'}{bold} | "
            f"bias {bold}{alert.current_structure_bias}{bold}"
        )
        if alert.nearest_level_name and alert.nearest_level_price is not None:
            body_lines.append(
                f"Nearest level: {bold}{alert.nearest_level_name} {alert.nearest_level_price:.2f}{bold} "
                f"({alert.distance_to_key_level_pct or 0:.2f}% away)"
            )
        if alert.structure_key_warning:
            body_lines.append(f"Structure warning: {alert.structure_key_warning}")
    if alert.pressure_label and alert.pressure_label != "UNKNOWN":
        score_text = f" {alert.pressure_score}" if alert.pressure_score is not None else ""
        body_lines.append(f"Pressure: {bold}{alert.pressure_label}{score_text}{bold}")
    if alert.option_feed_status and alert.option_feed_status != "UNAVAILABLE":
        body_lines.append(f"Option feed: {bold}{alert.option_feed_status}{bold}")
    if alert.risk_label == "DO_NOT_CHASE":
        body_lines.append(f"RISK: {bold}DO_NOT_CHASE — valid setup may be late{bold}")
    elif alert.risk_label:
        body_lines.append(f"Risk: {bold}{alert.risk_label}{bold}")
    body_lines.append(f"Entry timing: {bold}{alert.entry_timing_label or 'EARLY'}{bold}")
    body_lines.append(
        f"Invalidation: {bold}{alert.invalidation_level:.2f}{bold} — {alert.invalidation_reason}"
        if alert.invalidation_level is not None
        else f"Invalidation: {bold}WATCH ONLY{bold} — {alert.invalidation_reason}"
    )
    body_lines.append(f"Stop logic: {alert.stop_logic_description}")
    if alert.pullback_required:
        body_lines.append(f"Risk warning: {bold}Pullback/retest required before considering entry{bold}")
    if alert.do_not_chase_warning:
        body_lines.append(f"Risk warning: {bold}DO NOT CHASE{bold}")
    if alert.strategy_reasons:
        body_lines.append("Strategy reasons: " + " | ".join(alert.strategy_reasons[:5]))
    if alert.strategy_warnings:
        body_lines.append("Strategy warnings: " + " | ".join(alert.strategy_warnings[:4]))
    if alert.market_alignment:
        body_lines.append(f"Market alignment: {bold}{alert.market_alignment}{bold}")
    if alert.fast_move_pct is not None:
        body_lines.append(f"Fast move: {bold}{alert.fast_move_pct:+.2f}%{bold}")
    if alert.day_move_pct is not None:
        body_lines.append(f"Day move: {bold}{alert.day_move_pct:+.2f}%{bold}")
    if alert.relative_volume is not None:
        body_lines.append(f"Relative volume: {bold}{alert.relative_volume:.2f}x{bold}")
    if alert.premarket_high is not None:
        body_lines.append(f"Premarket high: {bold}{alert.premarket_high:.2f}{bold}")
    if alert.premarket_low is not None:
        body_lines.append(f"Premarket low: {bold}{alert.premarket_low:.2f}{bold}")
    if alert.opening_range_high is not None and alert.opening_range_low is not None:
        body_lines.append(
            f"Opening range: {bold}{alert.opening_range_low:.2f} - {alert.opening_range_high:.2f}{bold}"
        )
    if alert.option_contract:
        option_bits = [
            alert.option_contract,
            alert.option_quality or "",
            f"score {alert.options_score}" if alert.options_score is not None else "",
        ]
        body_lines.append("Option: " + bold + " | ".join(bit for bit in option_bits if bit) + bold)
        if alert.option_bid is not None and alert.option_ask is not None:
            body_lines.append(f"Bid/Ask: {bold}{alert.option_bid:.2f} / {alert.option_ask:.2f}{bold}")
        if alert.option_delta is not None:
            body_lines.append(f"Delta: {bold}{alert.option_delta:+.2f}{bold}")
        if alert.option_iv is not None:
            body_lines.append(f"IV: {bold}{alert.option_iv:.2%}{bold}")
    if alert.headline:
        body_lines.append(f"Headline: {alert.headline}")
    if alert.url:
        body_lines.append(alert.url)
    if alert.notes:
        body_lines.append("Notes: " + " | ".join(alert.notes))
    if alert.text_alert_reason:
        body_lines.append(f"Text alert filter: {alert.text_alert_reason}")
    body_lines.append("Confirm in Webull before taking action.")
    return "\n".join(body_lines)


def professional_telegram_message(alert: Alert, alert_type: str) -> str:
    apply_risk_invalidation(alert)
    assign_professional_alert_tier(alert)
    scenario = alert.scenario_top or {}
    setup = alert.setup_name or scenario.get("scenario_name") or alert.primary_setup or alert.category
    direction = alert.setup_direction or alert.scenario_direction or alert.direction or alert.strategy_direction or "MOMENTUM"
    option_quality = alert.option_quality_message or option_quality_message(alert.option_quality or alert.option_feed_status)
    market_bits = [str(alert.market_regime or "UNKNOWN").replace("_", " ")]
    if alert.spy_alignment:
        market_bits.append(f"SPY {alert.spy_alignment}")
    if alert.qqq_alignment:
        market_bits.append(f"QQQ {alert.qqq_alignment}")
    structure_text = alert.market_structure_summary
    if not structure_text:
        structure_bits = [
            f"1m {alert.trend_1m or 'UNKNOWN'}",
            f"5m {alert.trend_5m or 'UNKNOWN'}",
            f"15m {alert.trend_15m or 'UNKNOWN'}",
        ]
        if alert.current_structure_bias:
            structure_bits.append(f"bias {alert.current_structure_bias}")
        structure_text = " | ".join(structure_bits)
    invalidation = (
        f"{alert.invalidation_level:.2f} — {alert.invalidation_reason}"
        if alert.invalidation_level is not None
        else alert.invalidation_reason or "No clean invalidation — watch only"
    )
    watch = alert.setup_watch_text or confirmation_required_for_tier(alert)
    if alert.chop_mode_active:
        watch = "Clean break-and-hold outside the range with 5m confirmation and SPY/QQQ alignment."
        option_quality = "Do not touch options while setup is mixed/choppy."
    elif alert.missed_clean_entry:
        watch = "A new pullback/retest before considering continuation."
    if alert.news_context_present:
        watch = f"{watch} Fresh AAPL news present — context only. Confirm price reaction."
    main_risk = telegram_risk_warning_reason(alert)
    if not main_risk:
        main_risk = "none major; confirm manually"
    title_detail = setup
    if alert.phone_conclusion == "MIXED / NO TRADE":
        bias = str(alert.current_structure_bias or direction or "mixed").title()
        title_detail = f"{bias} bias, signals conflict"
    elif alert.phone_conclusion in {"DO NOT CHASE", "CONTEXT ONLY"}:
        title_detail = f"{str(direction).title()} Bias" if str(direction).upper() in {"BULLISH", "BEARISH"} else setup
    lines = [
        f"{alert.symbol} {alert.phone_conclusion} — {_short_phone_text(title_detail, 80)}",
        "",
        f"Why: {_short_phone_text(_telegram_reason(alert, scenario), 170)}",
        f"Market: {' | '.join(market_bits)}",
        f"Structure: {_short_phone_text(structure_text, 170)}",
        f"Risk: {_short_phone_text(main_risk, 140)}",
        f"Wait for: {_short_phone_text(watch, 170)}",
        f"Invalidation: {_short_phone_text(invalidation, 170)}",
        f"Option: {option_quality}",
        "",
        "Heads-up only — confirm manually. Not a buy/sell signal.",
    ]
    return "\n".join(lines)[:900]


class DiscordNotifier:
    def __init__(self, webhook_url: Optional[str], mention: str = "") -> None:
        self.webhook_url = webhook_url
        self.mention = mention.strip()

    def send(self, alert: Alert) -> None:
        message = format_alert_message(alert, markdown=True)
        if not self.webhook_url:
            logger.info("DISCORD DISABLED\n%s", message)
            return

        payload = {
            "content": self.mention or None,
            "embeds": [
                {
                    "title": f"{alert.symbol} Momentum Alert",
                    "description": message,
                    "color": 5763719,
                    "timestamp": alert.timestamp.astimezone(UTC).isoformat(),
                }
            ],
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Discord send failed: %s", exc)


def parse_phone_numbers(phone_numbers: Optional[str]) -> List[str]:
    raw = (phone_numbers or "").replace(";", ",")
    return [phone.strip() for phone in raw.split(",") if phone.strip()]


def normalize_phone_for_messages(phone_number: str) -> str:
    cleaned = "".join(ch for ch in phone_number if ch.isdigit() or ch == "+")
    if cleaned.startswith("+"):
        return cleaned
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    return digits or phone_number.strip()


class MessagesNotifier:
    def __init__(self, phone_numbers: Optional[str], send_watch: bool = False) -> None:
        self.phone_numbers = [normalize_phone_for_messages(phone) for phone in parse_phone_numbers(phone_numbers)]
        self.send_watch = send_watch

    def send(self, alert: Alert) -> None:
        if not self.phone_numbers:
            return
        can_send = alert.sms_allowed or (self.send_watch and alert.watch_allowed) or bool(alert.phase3_heads_up_sent)
        if not can_send:
            logger.info(
                "SMS skipped for %s %s grade=%s score=%s reason=%s",
                alert.symbol,
                alert.category,
                alert.alert_grade,
                alert.alert_score,
                alert.text_alert_reason,
            )
            return
        alert.sms_sent = False
        message = phase3_heads_up_message(alert) if alert.phase3_heads_up_sent else format_alert_message(alert, markdown=False)
        script = """
        on run argv
          set targetBuddy to item 1 of argv
          set alertText to item 2 of argv
          tell application "Messages"
            set targetService to 1st service whose service type = SMS
            set targetBuddy to buddy targetBuddy of targetService
            send alertText to targetBuddy
          end tell
        end run
        """
        for phone_number in self.phone_numbers:
            try:
                subprocess.run(["osascript", "-e", script, phone_number, message], check=True, timeout=20)
                alert.sms_sent = True
                logger.info("Messages alert sent to %s for %s %s", phone_number, alert.symbol, alert.category)
            except Exception as exc:
                logger.warning("Messages send failed for %s: %s", phone_number, exc)


class MacDesktopNotifier:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def send(self, alert: Alert) -> None:
        if not self.enabled or not (alert.sms_allowed or alert.watch_allowed):
            return
        level = "WATCH" if alert.watch_allowed and not alert.sms_allowed else "ALERT"
        title = f"{level} {alert.symbol} {alert.direction or 'Momentum'} {alert.alert_grade or ''}".strip()
        subtitle = f"${alert.price:.2f} | {alert.category}"
        details = []
        if alert.trigger_level is not None:
            details.append(f"Level {alert.trigger_level:.2f}")
        if alert.fast_move_pct is not None:
            details.append(f"Fast {alert.fast_move_pct:+.2f}%")
        if alert.day_move_pct is not None:
            details.append(f"Day {alert.day_move_pct:+.2f}%")
        if alert.relative_volume is not None:
            details.append(f"RVOL {alert.relative_volume:.2f}x")
        message = " | ".join(details) or "Confirm in Webull before taking action."
        script = """
        on run argv
          display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv) sound name "Glass"
        end run
        """
        try:
            subprocess.run(["osascript", "-e", script, title, subtitle, message], check=True, timeout=10)
        except Exception as exc:
            logger.warning("Mac desktop notification failed: %s", exc)


class PushoverNotifier:
    def __init__(self, app_token: Optional[str], user_key: Optional[str], enabled: bool = True) -> None:
        self.app_token = (app_token or "").strip()
        self.user_key = (user_key or "").strip()
        self.enabled = enabled

    def send(self, alert: Alert) -> None:
        if not self.enabled or not alert.sms_allowed:
            return
        if not self.app_token or not self.user_key:
            return
        title = f"{alert.symbol} {alert.direction or 'Momentum'} {alert.alert_grade or ''}".strip()
        message = format_alert_message(alert, markdown=False)
        payload = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": message[:1024],
            "priority": 1 if alert.alert_grade in {"A", "A+"} else 0,
            "sound": "cashregister",
        }
        try:
            resp = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Pushover send failed: %s", exc)


def redact_notification_error(error: Any, secrets: Optional[Iterable[str]] = None) -> str:
    text = str(error or "")
    values = [os.getenv("TELEGRAM_BOT_TOKEN", "").strip()]
    values.extend(str(value).strip() for value in (secrets or []))
    for value in values:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text[:500]


def append_notification_status(
    channel: str,
    alert_type: str,
    symbol: str,
    sent: bool,
    error: Any = "",
    message_preview: str = "",
    sms_sent: Optional[bool] = None,
    dedupe_blocked: bool = False,
    alert_source: Optional[str] = None,
    chat_id: Optional[str] = None,
    alert_tier: Optional[str] = None,
    alert_tier_reason: Optional[str] = None,
    message_source_path: Optional[str] = None,
    phone_conclusion: Optional[str] = None,
    phone_conclusion_reason: Optional[str] = None,
    mixed_signal_no_trade: bool = False,
    no_trade_reason: Optional[str] = None,
) -> None:
    identity = scanner_identity(load_config(None))
    payload = {
        "timestamp": now_utc().isoformat(),
        **identity,
        "channel": channel,
        "alert_type": alert_type,
        "alert_source": alert_source or alert_type,
        "message_source_path": message_source_path or alert_source or alert_type,
        "phone_conclusion": phone_conclusion,
        "phone_conclusion_reason": phone_conclusion_reason,
        "alert_decision_label": phone_conclusion,
        "mixed_signal_no_trade": bool(mixed_signal_no_trade),
        "no_trade_reason": no_trade_reason,
        "telegram_message_version": "CONCLUSION_FIRST_V2" if channel == "telegram" else None,
        "old_format_removed": channel == "telegram",
        "alert_tier": alert_tier,
        "alert_tier_reason": alert_tier_reason,
        "symbol": symbol,
        "sent": bool(sent),
        "sms_sent": sms_sent,
        "telegram_sent": bool(sent) if channel == "telegram" else None,
        "telegram_chat_id": "[REDACTED]" if channel == "telegram" else None,
        **(telegram_destination_metadata(chat_id) if channel == "telegram" else {}),
        "telegram_error": redact_notification_error(error) if channel == "telegram" else "",
        "dedupe_blocked": bool(dedupe_blocked),
        "error": redact_notification_error(error),
        "message_preview": message_preview[:300],
        "token_redacted": True,
    }
    try:
        with NOTIFICATION_STATUS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception as exc:
        logger.warning("Notification status log failed: %s", redact_notification_error(exc))


def send_telegram_message(
    *,
    token: str,
    chat_id: str,
    message: str,
    timeout_seconds: int,
    alert_type: str,
    alert_source: str,
    symbol: str,
    sms_sent: Optional[bool] = None,
    dedupe_blocked: bool = False,
    alert_tier: Optional[str] = None,
    alert_tier_reason: Optional[str] = None,
    message_source_path: Optional[str] = None,
    phone_conclusion: Optional[str] = None,
    phone_conclusion_reason: Optional[str] = None,
    mixed_signal_no_trade: bool = False,
    no_trade_reason: Optional[str] = None,
) -> Tuple[bool, str]:
    if not token or not chat_id:
        error = "Telegram bot token or chat ID is missing"
        append_notification_status(
            "telegram", alert_type, symbol, False, error, message, sms_sent, dedupe_blocked, alert_source, chat_id,
            alert_tier, alert_tier_reason, message_source_path, phone_conclusion, phone_conclusion_reason,
            mixed_signal_no_trade, no_trade_reason,
        )
        return False, error
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        append_notification_status(
            "telegram", alert_type, symbol, True, "", message, sms_sent, dedupe_blocked, alert_source, chat_id,
            alert_tier, alert_tier_reason, message_source_path, phone_conclusion, phone_conclusion_reason,
            mixed_signal_no_trade, no_trade_reason,
        )
        return True, ""
    except Exception as exc:
        error = redact_notification_error(exc, [token, chat_id])
        append_notification_status(
            "telegram", alert_type, symbol, False, error, message, sms_sent, dedupe_blocked, alert_source, chat_id,
            alert_tier, alert_tier_reason, message_source_path, phone_conclusion, phone_conclusion_reason,
            mixed_signal_no_trade, no_trade_reason,
        )
        return False, error


def latest_notification_status(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = config or load_config(None)
    notification_config = config.get("notifications", {})
    enabled = bool(notification_config.get("telegram_enabled", False))
    configured = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip())
    latest_telegram: Dict[str, Any] = {}
    latest_sent: Dict[str, Any] = {}
    if NOTIFICATION_STATUS_LOG.exists():
        for line in reversed(NOTIFICATION_STATUS_LOG.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("channel") == "telegram":
                if not latest_telegram:
                    latest_telegram = record
                if record.get("sent"):
                    latest_sent = record
                    break
    active_channels: List[str] = []
    if config.get("discord", {}).get("enabled", True) and os.getenv("DISCORD_WEBHOOK_URL"):
        active_channels.append("Discord")
    if notification_config.get("mac_desktop_enabled", True):
        active_channels.append("Desktop")
    if notification_config.get("pushover_enabled", True) and os.getenv("PUSHOVER_APP_TOKEN") and os.getenv("PUSHOVER_USER_KEY"):
        active_channels.append("Pushover")
    if notification_config.get("messages_enabled", False) and os.getenv("ALERT_SMS_PHONE"):
        active_channels.append("Messages")
    if enabled and configured:
        active_channels.append("Telegram")
    return {
        "telegram_enabled": enabled,
        "telegram_configured": configured,
        "last_telegram_alert_time": latest_sent.get("timestamp"),
        "last_telegram_error": latest_telegram.get("error") or "",
        "telegram_duplicate_blocked": "duplicate" in str(latest_telegram.get("error") or "").lower(),
        "active_alert_channels": active_channels,
    }


def claim_phase3_telegram_delivery(
    alert: Alert,
    dedupe_minutes: int,
    state_path: Path = PHASE3_TELEGRAM_DEDUPE_STATE,
) -> Tuple[bool, str, Optional[datetime]]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state: Dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        current = phase3_heads_up_record(alert)
        previous = state.get(alert.symbol.upper())
        allowed, reason, last_sent = phase3_heads_up_dedupe_decision(previous, current, dedupe_minutes)
        if allowed:
            state[alert.symbol.upper()] = current
            temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
            temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            temp_path.replace(state_path)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return allowed, reason, last_sent


def telegram_message_fingerprint(alert_type: str, alert: Alert, message: str) -> str:
    payload = "|".join(
        [
            alert.symbol.upper(),
            alert_type.upper(),
            str(alert.category or "").upper(),
            str(alert.direction or "").upper(),
            message,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def claim_telegram_delivery(
    alert_type: str,
    alert: Alert,
    message: str,
    dedupe_minutes: int,
    state_path: Path = TELEGRAM_DELIVERY_DEDUPE_STATE,
) -> Tuple[bool, str, Optional[datetime]]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    fingerprint = telegram_message_fingerprint(alert_type, alert, message)
    now = now_utc()
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state: Dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        previous = state.get(fingerprint)
        last_sent: Optional[datetime] = None
        if isinstance(previous, dict):
            try:
                last_sent = datetime.fromisoformat(str(previous.get("sent_at")))
            except (TypeError, ValueError):
                last_sent = None
        allowed = not last_sent or (now - last_sent).total_seconds() > dedupe_minutes * 60
        reason = "first Telegram delivery" if not last_sent else "Telegram delivery cooldown elapsed"
        if not allowed:
            reason = "duplicate blocked: identical Telegram message within cooldown"
        else:
            state[fingerprint] = {
                "sent_at": now.isoformat(),
                "symbol": alert.symbol.upper(),
                "alert_type": alert_type.upper(),
            }
            cutoff = now - timedelta(days=1)
            state = {
                key: value
                for key, value in state.items()
                if isinstance(value, dict)
                and datetime.fromisoformat(str(value.get("sent_at"))) >= cutoff
            }
            temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
            temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            temp_path.replace(state_path)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return allowed, reason, last_sent


def append_phase3_telegram_dedupe_block(alert: Alert) -> None:
    assign_professional_alert_tier(alert)
    payload = {
        "timestamp": now_utc().isoformat(),
        "symbol": alert.symbol,
        "alert_tier": alert.alert_tier,
        "alert_tier_reason": alert.alert_tier_reason,
        "alert_source": alert.alert_source,
        "message_source_path": alert.message_source_path,
        "phone_conclusion": alert.phone_conclusion,
        "phone_conclusion_reason": alert.phone_conclusion_reason,
        "plain_english_conclusion": alert.plain_english_conclusion,
        "alert_decision_label": alert.alert_decision_label,
        "alert_decision_explanation": alert.alert_decision_explanation,
        "mixed_signal_no_trade": alert.mixed_signal_no_trade,
        "no_trade_reason": alert.no_trade_reason,
        "telegram_message_version": alert.telegram_message_version,
        "old_format_removed": alert.old_format_removed,
        "direction": alert.scenario_direction or alert.direction,
        "top_scenario": alert.scenario_top,
        "scenario_stage": alert.scenario_stage,
        "scenario_score": alert.scenario_score,
        "phase3_heads_up_eligible": alert.phase3_heads_up_eligible,
        "phase3_heads_up_sent": False,
        "phase3_heads_up_block_reason": alert.phase3_heads_up_block_reason,
        "dedupe_key": alert.phase3_heads_up_dedupe_key,
        "message_fingerprint": alert.phase3_heads_up_message_fingerprint,
        "dedupe_blocked": True,
        "dedupe_reason": alert.phase3_heads_up_dedupe_reason,
        "last_sent_at": alert.phase3_heads_up_last_sent_time,
        "stock_only_heads_up_allowed": alert.stock_only_heads_up_allowed,
        "stock_only_heads_up_reason": alert.stock_only_heads_up_reason,
        "phase3_heads_up_final_decision": alert.phase3_heads_up_final_decision,
        "phase3_heads_up_final_block_reason": alert.phase3_heads_up_final_block_reason,
        "watch_only_late_move": alert.watch_only_late_move,
        "do_not_chase_watch": alert.do_not_chase_watch,
        "context_symbols_available": alert.context_symbols_available,
        "market_confirmation_status": alert.market_confirmation_status,
        "telegram_attempted": False,
        "telegram_sent": False,
        "telegram_error": "",
    }
    try:
        with (LOG_DIR / "phase3_heads_up.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception as exc:
        logger.warning("Phase 3 dedupe log failed: %s", redact_notification_error(exc))


def append_phase3_telegram_delivery(alert: Alert, sent: bool, error: Any = "") -> None:
    if not alert.phase3_heads_up_eligible:
        return
    alert.phase3_heads_up_final_decision = "SENT" if sent else "TELEGRAM_FAILED"
    alert.phase3_heads_up_final_block_reason = "" if sent else redact_notification_error(error)
    assign_professional_alert_tier(alert)
    payload = {
        "timestamp": now_utc().isoformat(),
        "symbol": alert.symbol,
        "alert_tier": alert.alert_tier,
        "alert_tier_reason": alert.alert_tier_reason,
        "alert_source": alert.alert_source,
        "message_source_path": alert.message_source_path,
        "phone_conclusion": alert.phone_conclusion,
        "phone_conclusion_reason": alert.phone_conclusion_reason,
        "plain_english_conclusion": alert.plain_english_conclusion,
        "alert_decision_label": alert.alert_decision_label,
        "alert_decision_explanation": alert.alert_decision_explanation,
        "mixed_signal_no_trade": alert.mixed_signal_no_trade,
        "no_trade_reason": alert.no_trade_reason,
        "telegram_message_version": alert.telegram_message_version,
        "old_format_removed": alert.old_format_removed,
        "phase3_heads_up_final_decision": "SENT" if sent else "TELEGRAM_FAILED",
        "phase3_heads_up_final_block_reason": "" if sent else redact_notification_error(error),
        "stock_only_heads_up_allowed": alert.stock_only_heads_up_allowed,
        "stock_only_heads_up_reason": alert.stock_only_heads_up_reason,
        "watch_only_late_move": alert.watch_only_late_move,
        "do_not_chase_watch": alert.do_not_chase_watch,
        "market_context_missing_warning": alert.market_context_missing_warning,
        "option_stale_did_not_block_heads_up": alert.option_stale_did_not_block_heads_up,
        "context_symbols_expected": alert.context_symbols_expected,
        "context_symbols_available": alert.context_symbols_available,
        "telegram_attempted": True,
        "telegram_sent": bool(sent),
        "telegram_error": redact_notification_error(error),
    }
    try:
        with (LOG_DIR / "phase3_heads_up.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception as exc:
        logger.warning("Phase 3 Telegram delivery log failed: %s", redact_notification_error(exc))


class TelegramNotifier:
    def __init__(
        self,
        bot_token: Optional[str],
        chat_id: Optional[str],
        enabled: bool = False,
        alert_types: Optional[Iterable[str]] = None,
        aapl_only: bool = True,
        timeout_seconds: int = 8,
        phase3_dedupe_minutes: int = 15,
        phase3_dedupe_state_path: Path = PHASE3_TELEGRAM_DEDUPE_STATE,
        delivery_dedupe_minutes: int = 15,
        delivery_dedupe_state_path: Path = TELEGRAM_DELIVERY_DEDUPE_STATE,
        openai_formatter_enabled: bool = False,
        openai_formatter_style: str = "section",
        openai_formatter_fallback: bool = True,
        openai_formatter_max_chars: int = 900,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = enabled
        self.alert_types = {str(value).strip().upper() for value in (alert_types or []) if str(value).strip()}
        self.aapl_only = aapl_only
        self.timeout_seconds = timeout_seconds
        self.phase3_dedupe_minutes = phase3_dedupe_minutes
        self.phase3_dedupe_state_path = phase3_dedupe_state_path
        self.delivery_dedupe_minutes = delivery_dedupe_minutes
        self.delivery_dedupe_state_path = delivery_dedupe_state_path
        self.openai_formatter_enabled = openai_formatter_enabled
        self.openai_formatter_style = str(openai_formatter_style or "section").lower()
        self.openai_formatter_fallback = openai_formatter_fallback
        self.openai_formatter_max_chars = max(300, min(900, int(openai_formatter_max_chars)))

    def format_message(self, alert: Alert, alert_type: str, rule_message: Optional[str] = None) -> str:
        rule_message = rule_message or professional_telegram_message(alert, alert_type)
        if not self.openai_formatter_enabled or self.openai_formatter_style != "section":
            return rule_message
        try:
            from tools import preview_alert_text

            conclusion_cases = {
                "MIXED / NO TRADE": "mixed",
                "DO NOT CHASE": "do_not_chase",
                "WATCH ONLY": "watch_only",
                "TRADE QUALITY WATCH": "trade_quality",
                "CONTEXT ONLY": "context",
                "RISK WARNING": "risk_warning",
            }
            case_name = conclusion_cases.get(str(alert.phone_conclusion or "").upper(), "watch_only")
            result = preview_alert_text.format_with_openai(
                case_name,
                alert,
                rule_message,
                max_chars=self.openai_formatter_max_chars,
            )
            return str(result.get("message") or rule_message)
        except Exception as exc:
            logger.warning("OpenAI alert formatter failed safely: %s", redact_notification_error(exc))
            return rule_message

    def alert_type_for(self, alert: Alert) -> Optional[str]:
        if alert.phase3_heads_up_sent:
            return "PHASE3_HEADS_UP"
        if alert.sms_allowed:
            return "NORMAL_SMS"
        if alert.watch_allowed:
            return "NORMAL_WATCH"
        return None

    def alert_type_enabled(self, alert_type: str) -> bool:
        if alert_type == "PHASE3_HEADS_UP" and "STOCK_ONLY_WARNING" in self.alert_types:
            return "PHASE3_HEADS_UP" in self.alert_types or "STOCK_ONLY_WARNING" in self.alert_types
        if alert_type == "NORMAL_WATCH":
            return "NORMAL_WATCH" in self.alert_types or "NORMAL_SMS" in self.alert_types
        return alert_type in self.alert_types

    def send(self, alert: Alert) -> None:
        if not self.enabled:
            return
        alert_type = self.alert_type_for(alert)
        if not alert_type or not self.alert_type_enabled(alert_type):
            return
        if self.aapl_only and alert.symbol.upper() != "AAPL":
            return
        assign_professional_alert_tier(alert)
        rule_message = professional_telegram_message(alert, alert_type)
        message = self.format_message(alert, alert_type, rule_message)
        if not self.bot_token or not self.chat_id:
            _, error = send_telegram_message(
                token=self.bot_token,
                chat_id=self.chat_id,
                message=message,
                timeout_seconds=self.timeout_seconds,
                alert_type=alert_type,
                alert_source=alert.alert_source or alert_type,
                symbol=alert.symbol,
                sms_sent=alert.sms_sent,
                alert_tier=alert.alert_tier,
                alert_tier_reason=alert.alert_tier_reason,
                message_source_path=alert.message_source_path,
                phone_conclusion=alert.phone_conclusion,
                phone_conclusion_reason=alert.phone_conclusion_reason,
                mixed_signal_no_trade=alert.mixed_signal_no_trade,
                no_trade_reason=alert.no_trade_reason,
            )
            append_phase3_telegram_delivery(alert, False, error)
            return
        if alert_type == "PHASE3_HEADS_UP":
            allowed, reason, last_sent = claim_phase3_telegram_delivery(
                alert,
                self.phase3_dedupe_minutes,
                self.phase3_dedupe_state_path,
            )
            alert.phase3_heads_up_dedupe_key = phase3_heads_up_dedupe_key(alert)
            alert.phase3_heads_up_message_fingerprint = phase3_heads_up_message_fingerprint(alert)
            alert.phase3_heads_up_dedupe_reason = reason
            if not allowed:
                alert.phase3_heads_up_sent = False
                alert.phase3_heads_up_dedupe_blocked = True
                alert.phase3_heads_up_block_reason = reason
                alert.phase3_heads_up_last_sent_time = last_sent.isoformat() if last_sent else None
                append_phase3_telegram_dedupe_block(alert)
                append_notification_status(
                    "telegram",
                    alert_type,
                    alert.symbol,
                    False,
                    f"duplicate blocked: {reason}",
                    message,
                    sms_sent=alert.sms_sent,
                    dedupe_blocked=True,
                    alert_source=alert.alert_source,
                    alert_tier=alert.alert_tier,
                    alert_tier_reason=alert.alert_tier_reason,
                    message_source_path=alert.message_source_path,
                    phone_conclusion=alert.phone_conclusion,
                    phone_conclusion_reason=alert.phone_conclusion_reason,
                    mixed_signal_no_trade=alert.mixed_signal_no_trade,
                    no_trade_reason=alert.no_trade_reason,
                )
                append_phase3_telegram_delivery(alert, False, reason)
                return
        allowed, reason, _ = claim_telegram_delivery(
            alert_type,
            alert,
            rule_message,
            self.delivery_dedupe_minutes,
            self.delivery_dedupe_state_path,
        )
        if not allowed:
            append_notification_status(
                "telegram",
                alert_type,
                alert.symbol,
                False,
                reason,
                message,
                sms_sent=alert.sms_sent,
                dedupe_blocked=True,
                alert_source=alert.alert_source,
                alert_tier=alert.alert_tier,
                alert_tier_reason=alert.alert_tier_reason,
                message_source_path=alert.message_source_path,
                phone_conclusion=alert.phone_conclusion,
                phone_conclusion_reason=alert.phone_conclusion_reason,
                mixed_signal_no_trade=alert.mixed_signal_no_trade,
                no_trade_reason=alert.no_trade_reason,
            )
            append_phase3_telegram_delivery(alert, False, reason)
            return
        alert_source = (
            "STOCK_ONLY_WARNING"
            if alert_type == "PHASE3_HEADS_UP" and alert.stock_only_heads_up_allowed
            else alert_type
        )
        sent, error = send_telegram_message(
            token=self.bot_token,
            chat_id=self.chat_id,
            message=message,
            timeout_seconds=self.timeout_seconds,
            alert_type=alert_type,
            alert_source=alert.alert_source or alert_source,
            symbol=alert.symbol,
            sms_sent=alert.sms_sent,
            alert_tier=alert.alert_tier,
            alert_tier_reason=alert.alert_tier_reason,
            message_source_path=alert.message_source_path,
            phone_conclusion=alert.phone_conclusion,
            phone_conclusion_reason=alert.phone_conclusion_reason,
            mixed_signal_no_trade=alert.mixed_signal_no_trade,
            no_trade_reason=alert.no_trade_reason,
        )
        if sent:
            append_phase3_telegram_delivery(alert, True)
        else:
            append_phase3_telegram_delivery(alert, False, error)
            logger.warning("Telegram send failed for %s: %s", alert.symbol, error)


def send_telegram_test_message(config: Optional[Dict[str, Any]] = None) -> bool:
    config = config or load_config(None)
    notification_config = config.get("notifications", {})
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    message = "Test alert from AAPL scanner — Telegram notifications are working."
    sent, error = send_telegram_message(
        token=token,
        chat_id=chat_id,
        message=message,
        timeout_seconds=int(notification_config.get("telegram_timeout_seconds", 8)),
        alert_type="TEST",
        alert_source="TEST",
        symbol="AAPL",
    )
    if not sent:
        logger.warning("Telegram test failed: %s", error)
    return sent


class CompositeNotifier:
    def __init__(self, notifiers: List[Any]) -> None:
        self.notifiers = notifiers

    def send(self, alert: Alert) -> None:
        for notifier in self.notifiers:
            notifier.send(alert)


def make_notifier(config: Dict[str, Any]) -> CompositeNotifier:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    phone_number = os.getenv("ALERT_SMS_PHONE")
    pushover_token = os.getenv("PUSHOVER_APP_TOKEN")
    pushover_user = os.getenv("PUSHOVER_USER_KEY")
    notification_config = config.get("notifications", {})
    telegram_alert_types = notification_config.get(
        "telegram_alert_types",
        ["PHASE3_HEADS_UP", "STOCK_ONLY_WARNING", "NORMAL_WATCH", "NORMAL_SMS"],
    )
    if isinstance(telegram_alert_types, str):
        telegram_alert_types = [part.strip() for part in telegram_alert_types.split(",") if part.strip()]
    return CompositeNotifier([
        DiscordNotifier(
            webhook_url=webhook_url if config["discord"]["enabled"] else None,
            mention=config["discord"].get("mention", ""),
        ),
        MacDesktopNotifier(bool(notification_config.get("mac_desktop_enabled", True))),
        PushoverNotifier(
            pushover_token,
            pushover_user,
            enabled=bool(notification_config.get("pushover_enabled", True)),
        ),
        MessagesNotifier(
            phone_number if notification_config.get("messages_enabled", False) else None,
            send_watch=bool(notification_config.get("messages_watch_enabled", False)),
        ),
        TelegramNotifier(
            os.getenv("TELEGRAM_BOT_TOKEN"),
            os.getenv("TELEGRAM_CHAT_ID"),
            enabled=bool(notification_config.get("telegram_enabled", False)),
            alert_types=telegram_alert_types,
            aapl_only=bool(notification_config.get("telegram_aapl_only", True)),
            timeout_seconds=int(notification_config.get("telegram_timeout_seconds", 8)),
            phase3_dedupe_minutes=int(config.get("scenario_engine", {}).get("phase3_heads_up_dedupe_minutes", 15)),
            delivery_dedupe_minutes=int(config.get("scenario_engine", {}).get("phase3_heads_up_dedupe_minutes", 15)),
            openai_formatter_enabled=bool(notification_config.get("openai_alert_formatter_enabled", True)),
            openai_formatter_style=str(notification_config.get("openai_alert_formatter_style", "section")),
            openai_formatter_fallback=bool(notification_config.get("openai_alert_formatter_fallback", True)),
            openai_formatter_max_chars=int(notification_config.get("openai_alert_formatter_max_chars", 900)),
        ),
    ])


class _LegacyDiscordNotifier:
    def __init__(self, webhook_url: Optional[str], mention: str = "") -> None:
        self.webhook_url = webhook_url
        self.mention = mention.strip()

    def send(self, alert: Alert) -> None:
        body_lines = [
            f"**{alert.symbol}** @ **${alert.price:.2f}**",
            f"Category: **{alert.category}**",
        ]
        if alert.fast_move_pct is not None:
            body_lines.append(f"Fast move: **{alert.fast_move_pct:+.2f}%**")
        if alert.day_move_pct is not None:
            body_lines.append(f"Day move: **{alert.day_move_pct:+.2f}%**")
        if alert.relative_volume is not None:
            body_lines.append(f"Relative volume: **{alert.relative_volume:.2f}x**")
        if alert.premarket_high is not None:
            body_lines.append(f"Premarket high: **{alert.premarket_high:.2f}**")
        if alert.premarket_low is not None:
            body_lines.append(f"Premarket low: **{alert.premarket_low:.2f}**")
        if alert.opening_range_high is not None and alert.opening_range_low is not None:
            body_lines.append(
                f"Opening range: **{alert.opening_range_low:.2f} – {alert.opening_range_high:.2f}**"
            )
        if alert.option_contract:
            option_bits = [
                alert.option_contract,
                alert.option_quality or "",
                f"score {alert.options_score}" if alert.options_score is not None else "",
            ]
            body_lines.append("Option: **" + " | ".join(bit for bit in option_bits if bit) + "**")
            if alert.option_bid is not None and alert.option_ask is not None:
                body_lines.append(
                    f"Bid/Ask: **{alert.option_bid:.2f} / {alert.option_ask:.2f}**"
                )
            if alert.option_delta is not None:
                body_lines.append(f"Delta: **{alert.option_delta:+.2f}**")
            if alert.option_iv is not None:
                body_lines.append(f"IV: **{alert.option_iv:.2%}**")
        if alert.headline:
            body_lines.append(f"Headline: {alert.headline}")
        if alert.url:
            body_lines.append(alert.url)
        if alert.notes:
            body_lines.append("Notes: " + " | ".join(alert.notes))
        body_lines.append("Watch for continuation or rejection before taking action.")

        message = "\n".join(body_lines)
        if not self.webhook_url:
            logger.info("DISCORD DISABLED\n%s", message)
            return

        payload = {
            "content": self.mention or None,
            "embeds": [
                {
                    "title": f"{alert.symbol} Momentum Alert",
                    "description": message,
                    "color": 5763719,
                    "timestamp": alert.timestamp.astimezone(UTC).isoformat(),
                }
            ],
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Discord send failed: %s", exc)


# ------------------------------------------------------------
# Session helpers
# ------------------------------------------------------------
def in_extended_or_regular_session(config: Dict[str, Any]) -> bool:
    t = now_et()
    pre = set_today_time_et(config["premarket_start"])
    post = set_today_time_et(config["postmarket_end"])
    return pre <= t <= post


def in_regular_session(config: Dict[str, Any]) -> bool:
    t = now_et()
    open_t = set_today_time_et(config["market_open"])
    close_t = set_today_time_et(config["market_close"])
    return open_t <= t <= close_t


def in_premarket(config: Dict[str, Any]) -> bool:
    t = now_et()
    pre = set_today_time_et(config["premarket_start"])
    open_t = set_today_time_et(config["market_open"])
    return pre <= t < open_t


def in_postmarket(config: Dict[str, Any]) -> bool:
    t = now_et()
    close_t = set_today_time_et(config["market_close"])
    post = set_today_time_et(config["postmarket_end"])
    return close_t < t <= post


def is_opening_range_bar(bar_time: datetime, config: Dict[str, Any]) -> bool:
    return is_opening_range_bar_for_minutes(bar_time, config, int(config["opening_range_minutes"]))


def is_opening_range_bar_for_minutes(bar_time: datetime, config: Dict[str, Any], minutes: int) -> bool:
    bar_et = bar_time.astimezone(ET)
    start = set_today_time_et(config["market_open"])
    end = start + timedelta(minutes=int(minutes))
    return start <= bar_et < end


def is_premarket_bar(bar_time: datetime, config: Dict[str, Any]) -> bool:
    bar_et = bar_time.astimezone(ET)
    start = set_today_time_et(config["premarket_start"])
    open_t = set_today_time_et(config["market_open"])
    return start <= bar_et < open_t


def session_history_start(config: Dict[str, Any]) -> datetime:
    pre = set_today_time_et(config["premarket_start"])
    return pre.astimezone(UTC)


def session_anchor_bar(bars: List[Bar], config: Dict[str, Any]) -> Optional[Bar]:
    if not bars:
        return None
    open_t = set_today_time_et(config["market_open"])
    for bar in bars:
        if bar.t.astimezone(ET) >= open_t:
            return bar
    return bars[0]


def bar_age_minutes(bar: Optional[Bar]) -> Optional[float]:
    if not bar:
        return None
    return max(0.0, (now_utc() - bar.t.astimezone(UTC)).total_seconds() / 60.0)


def is_stale_bar(bar: Optional[Bar], config: Dict[str, Any]) -> bool:
    age = bar_age_minutes(bar)
    if age is None:
        return True
    stale_after = float(config.get("data_quality", {}).get("stale_after_minutes", 20))
    return age > stale_after


def has_min_recent_bars(bars: List[Bar], config: Dict[str, Any]) -> bool:
    min_bars = int(config.get("data_quality", {}).get("min_recent_bars", 10))
    lookback_bars = int(config["lookback_minutes_fast_move"]) + 1
    return len(bars) >= max(min_bars, lookback_bars)


def opening_range_complete(bars: List[Bar], config: Dict[str, Any]) -> bool:
    required = int(config["opening_range_minutes"])
    return len([b for b in bars if is_opening_range_bar(b.t, config)]) >= required


def snapshot_data_quality(snap: SymbolSnapshot, config: Dict[str, Any]) -> str:
    if not snap.latest_bar:
        return "Incomplete"
    if is_stale_bar(snap.latest_bar, config):
        return "Stale"
    if not has_min_recent_bars(snap.recent_bars, config):
        return "Incomplete"
    day_volume = sum(b.v for b in snap.recent_bars)
    if day_volume < config["filters"]["min_day_volume"]:
        return "Low volume"
    return "Fresh"


def option_quote_age_seconds(
    contract: OptionContractSnapshot,
    scanner_time: Optional[datetime] = None,
) -> Optional[float]:
    quote_time = parse_optional_dt(contract.quote_time)
    if not quote_time:
        return None
    current = parse_optional_dt(scanner_time or now_utc()) or now_utc()
    return max(0.0, (current - quote_time).total_seconds())


def options_session_active(at: Optional[datetime] = None) -> bool:
    current = (parse_optional_dt(at or now_utc()) or now_utc()).astimezone(ET)
    return current.weekday() < 5 and datetime_time(9, 30) <= current.time() <= datetime_time(16, 0)


def option_freshness_details(
    contract: OptionContractSnapshot,
    config: Dict[str, Any],
    scanner_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = parse_optional_dt(scanner_time or now_utc()) or now_utc()
    quote_time = parse_optional_dt(contract.quote_time)
    fallback_time = parse_optional_dt(contract.timestamp_fallback_time or contract.trade_time) if quote_time is None else None
    max_age = float(config.get("options", {}).get("max_quote_age_seconds", 60))
    age = option_quote_age_seconds(contract, current)
    fallback_age = max(0.0, (current - fallback_time).total_seconds()) if fallback_time else None
    reason = ""
    invalid_reason = ""
    status = "recent"
    if contract.bid is None or contract.ask is None or contract.bid <= 0 or contract.ask <= 0 or contract.ask <= contract.bid:
        status = "invalid"
        invalid_reason = "missing_bid_or_ask"
    elif contract.spread_pct is None or contract.spread_pct > float(config.get("options", {}).get("max_spread_pct", 12)):
        status = "poor_quality"
        reason = "wide_spread"
    elif quote_time is None:
        if fallback_age is not None and fallback_age <= max_age:
            status = "diagnostic"
            reason = "quote_timestamp_missing_recent_activity_fallback"
        else:
            status = "stale"
            reason = "missing_timestamp"
    elif age is not None and age > max_age:
        status = "stale"
        reason = "session_closed_or_inactive" if not options_session_active(current) else "quote_age_exceeded"
    return {
        "status": status,
        "stale_reason": reason,
        "invalid_reason": invalid_reason,
        "quote_timestamp_raw": contract.quote_timestamp_raw or (str(contract.quote_time) if contract.quote_time is not None else None),
        "quote_timestamp_utc": quote_time.isoformat() if quote_time else None,
        "scanner_timestamp_utc": current.isoformat(),
        "quote_age_seconds": round(age, 3) if age is not None else None,
        "max_allowed_quote_age_seconds": max_age,
        "options_session_active": options_session_active(current),
        "market_session_status": "active" if options_session_active(current) else "session_closed_or_inactive",
        "timestamp_source_field": contract.quote_timestamp_source_field,
        "timestamp_extraction_failed": contract.timestamp_extraction_failed,
        "timestamp_available_fields": contract.timestamp_available_fields,
        "fallback_used": quote_time is None and fallback_time is not None,
        "fallback_type": contract.timestamp_fallback_type or ("latest_trade" if quote_time is None and fallback_time else None),
        "fallback_timestamp_utc": fallback_time.isoformat() if fallback_time else None,
        "fallback_age_seconds": round(fallback_age, 3) if fallback_age is not None else None,
    }


def option_expiration_rank(expiration: date, config: Dict[str, Any]) -> Optional[int]:
    today = now_et().date()
    if expiration < today:
        return None
    mode = config.get("options", {}).get("expiry_mode", "0dte_then_weekly")
    if mode == "0dte_only" and expiration != today:
        return None
    if mode == "up_to_7dte" and (expiration - today).days > 7:
        return None
    return 0 if expiration == today else (expiration - today).days


OPTION_QUALITY_LABELS = {
    "TRADABLE",
    "POOR_QUALITY",
    "WIDE_SPREAD",
    "STALE",
    "INVALID",
    "TOO_RISKY_0DTE",
    "LOW_LIQUIDITY",
    "WATCH_ONLY",
}

OPTION_QUALITY_ALIASES = {
    "TRADABLE": "TRADABLE",
    "TRADABLE DIAGNOSTIC": "WATCH_ONLY",
    "WIDE SPREAD": "WIDE_SPREAD",
    "STALE QUOTE": "STALE",
    "INVALID QUOTE": "INVALID",
    "LOW LIQUIDITY": "LOW_LIQUIDITY",
    "NO CLEAN CONTRACT": "POOR_QUALITY",
}


def normalize_option_quality_label(label: Optional[str]) -> str:
    value = str(label or "").strip().upper().replace("-", "_")
    if value in OPTION_QUALITY_LABELS:
        return value
    return OPTION_QUALITY_ALIASES.get(value.replace("_", " "), value or "INVALID")


def option_quality_is_tradable(label: Optional[str]) -> bool:
    return normalize_option_quality_label(label) == "TRADABLE"


def option_quality_message(label: Optional[str], is_0dte: bool = False) -> str:
    normalized = normalize_option_quality_label(label)
    messages = {
        "TRADABLE": "Option tradable",
        "WIDE_SPREAD": "Option wide spread — stock setup only",
        "STALE": "Option stale — stock setup only",
        "INVALID": "Option invalid — stock setup only",
        "LOW_LIQUIDITY": "Option low liquidity — stock setup only",
        "POOR_QUALITY": "Option poor quality — stock setup only",
        "WATCH_ONLY": "Option watch only — stock setup only",
        "TOO_RISKY_0DTE": "0DTE caution — stock setup only",
    }
    message = messages.get(normalized, "Option unavailable — stock setup only")
    if normalized == "TRADABLE" and is_0dte:
        message += " | 0DTE caution"
    return message


def evaluate_option_quality(
    contract: OptionContractSnapshot,
    config: Dict[str, Any],
    underlying_price: Optional[float] = None,
    scanner_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    options_config = config.get("options", {})
    reasons: List[str] = []
    current = parse_optional_dt(scanner_time or now_utc()) or now_utc()
    freshness = option_freshness_details(contract, config, current)
    days_to_expiration = (contract.expiration_date - current.astimezone(ET).date()).days
    is_0dte = days_to_expiration == 0
    strike_distance_pct = (
        abs(contract.strike - underlying_price) / underlying_price * 100.0
        if underlying_price is not None and underlying_price > 0
        else None
    )
    min_volume = int(options_config.get("min_option_volume", 100))
    min_oi = int(options_config.get("min_open_interest", 250))
    volume_low = contract.volume is not None and contract.volume < min_volume
    oi_low = contract.open_interest is not None and contract.open_interest < min_oi
    liquidity_state = "LOW" if volume_low or oi_low else "ACCEPTABLE"
    time_state = "REGULAR_SESSION" if options_session_active(current) else "SESSION_CLOSED_OR_INACTIVE"
    label = "TRADABLE"

    if freshness["status"] == "invalid":
        label, reasons = "INVALID", [freshness["invalid_reason"]]
    elif freshness["status"] == "stale":
        label, reasons = "STALE", [freshness["stale_reason"]]
    elif freshness["status"] == "poor_quality":
        label, reasons = "WIDE_SPREAD", [freshness["stale_reason"]]
    elif freshness["status"] == "diagnostic":
        label, reasons = "WATCH_ONLY", [freshness["stale_reason"]]
    elif volume_low or oi_low:
        label = "LOW_LIQUIDITY"
        if volume_low:
            reasons.append("option volume is below minimum")
        if oi_low:
            reasons.append("open interest is below minimum")
    elif contract.delta is None:
        label, reasons = "POOR_QUALITY", ["delta is missing"]
    else:
        delta_abs = abs(contract.delta)
        if delta_abs < float(options_config.get("delta_min", 0.30)) or delta_abs > float(options_config.get("delta_max", 0.60)):
            label, reasons = "POOR_QUALITY", ["delta is outside target range"]

    risky_0dte_after = str(options_config.get("risky_0dte_after_et", "15:30"))
    try:
        risky_hour, risky_minute = (int(part) for part in risky_0dte_after.split(":", 1))
        late_0dte = current.astimezone(ET).time() >= datetime_time(risky_hour, risky_minute)
    except (TypeError, ValueError):
        late_0dte = False
    if (
        label == "TRADABLE"
        and is_0dte
        and late_0dte
        and bool(options_config.get("block_late_0dte", False))
    ):
        label, reasons = "TOO_RISKY_0DTE", ["0DTE contract is inside configured late-session risk window"]

    return {
        "label": label,
        "message": option_quality_message(label, is_0dte=is_0dte),
        "reasons": reasons,
        "bid": contract.bid,
        "ask": contract.ask,
        "mid": contract.mid,
        "spread_pct": contract.spread_pct,
        "quote_age_seconds": freshness["quote_age_seconds"],
        "timestamp_source_field": freshness["timestamp_source_field"],
        "expiration": contract.expiration_date.isoformat(),
        "days_to_expiration": days_to_expiration,
        "is_0dte": is_0dte,
        "strike_distance_pct": round(strike_distance_pct, 3) if strike_distance_pct is not None else None,
        "liquidity_state": liquidity_state,
        "time_state": time_state,
        "trade_ready_allowed": label == "TRADABLE",
        "stock_only_allowed": True,
    }


def option_contract_quality(contract: OptionContractSnapshot, config: Dict[str, Any]) -> Tuple[str, List[str]]:
    result = evaluate_option_quality(contract, config)
    return result["label"], result["reasons"]


def score_option_contract(contract: OptionContractSnapshot, quality: str, config: Dict[str, Any]) -> int:
    if not option_quality_is_tradable(quality):
        return 0
    score = 40.0
    spread_pct = contract.spread_pct or float(config.get("options", {}).get("max_spread_pct", 12))
    max_spread = max(float(config.get("options", {}).get("max_spread_pct", 12)), 0.01)
    score += max(0.0, (1.0 - spread_pct / max_spread)) * 25.0
    if contract.volume is not None:
        score += min(15.0, contract.volume / max(int(config.get("options", {}).get("min_option_volume", 100)), 1) * 7.5)
    if contract.open_interest is not None:
        score += min(10.0, contract.open_interest / max(int(config.get("options", {}).get("min_open_interest", 250)), 1) * 5.0)
    if contract.delta is not None:
        score += max(0.0, 10.0 - abs(abs(contract.delta) - 0.45) * 40.0)
    return int(round(max(0.0, min(100.0, score))))


def choose_best_option_contract(
    chain: List[OptionContractSnapshot],
    option_type: str,
    underlying_price: float,
    config: Dict[str, Any],
) -> OptionSelection:
    candidates = [
        contract for contract in chain
        if contract.option_type == option_type and option_expiration_rank(contract.expiration_date, config) is not None
    ]
    if not candidates:
        return OptionSelection(quality="INVALID", reasons=["no matching expiration"])

    best_any: Optional[OptionSelection] = None
    best_tradable: Optional[OptionSelection] = None
    for contract in candidates:
        details = evaluate_option_quality(contract, config, underlying_price=underlying_price)
        quality, reasons = details["label"], details["reasons"]
        score = score_option_contract(contract, quality, config)
        expiry_rank = option_expiration_rank(contract.expiration_date, config) or 0
        distance_penalty = abs(contract.strike - underlying_price)
        selection = OptionSelection(contract=contract, quality=quality, score=score, reasons=reasons, details=details)
        sort_key = (-expiry_rank, score, -distance_penalty)
        if option_quality_is_tradable(quality):
            if best_tradable is None:
                best_tradable = selection
            else:
                current = best_tradable.contract
                assert current is not None
                current_key = (-(option_expiration_rank(current.expiration_date, config) or 0), best_tradable.score, -abs(current.strike - underlying_price))
                if sort_key > current_key:
                    best_tradable = selection
        if best_any is None:
            best_any = selection
        else:
            current = best_any.contract
            assert current is not None
            current_quality_rank = 1 if option_quality_is_tradable(best_any.quality) else 0
            quality_rank = 1 if option_quality_is_tradable(quality) else 0
            current_key = (current_quality_rank, -(option_expiration_rank(current.expiration_date, config) or 0), best_any.score, -abs(current.strike - underlying_price))
            any_key = (quality_rank, -expiry_rank, score, -distance_penalty)
            if any_key > current_key:
                best_any = selection

    return best_tradable or best_any or OptionSelection(quality="INVALID")


def select_option_contracts(
    chain: List[OptionContractSnapshot],
    underlying_price: Optional[float],
    config: Dict[str, Any],
) -> Tuple[OptionSelection, OptionSelection]:
    if underlying_price is None or not config.get("options", {}).get("enabled", True):
        empty = OptionSelection(quality="INVALID", reasons=["options disabled or missing underlying price"])
        return empty, empty
    return (
        choose_best_option_contract(chain, "C", underlying_price, config),
        choose_best_option_contract(chain, "P", underlying_price, config),
    )


# ------------------------------------------------------------
# Scanner engine
# ------------------------------------------------------------
class EliteScanner:
    def __init__(
        self,
        config: Dict[str, Any],
        provider: DataProvider,
        notifier: Any,
        writer: AlertWriter,
        state_store: StateStore,
    ) -> None:
        self.config = config
        self.provider = provider
        self.notifier = notifier
        self.writer = writer
        self.state_store = state_store
        self.symbols = list(config["symbols"])
        if config.get("only_symbols_with_options", True):
            eligible = set(config.get("symbols_with_options", []))
            self.symbols = [s for s in self.symbols if s in eligible]
        self.context_symbols = [symbol for symbol in self.market_context_symbols() if symbol not in self.symbols]
        self.data_symbols = list(dict.fromkeys(self.symbols + self.context_symbols))
        performance_config = config.get("post_alert_performance", {})
        self.post_alert_performance_enabled = bool(performance_config.get("enabled", True))
        self.post_alert_tracker = PostAlertPerformanceTracker(
            writer.jsonl_path.parent / "post_alert_performance.jsonl",
            state_store.path.parent / "post_alert_performance_pending.json",
            intervals=performance_config.get("interval_minutes", [1, 3, 5, 10, 15]),
            target_move_pct=float(performance_config.get("target_move_pct", 0.30)),
        )
        self.decision_history: List[Dict[str, Any]] = []
        self.last_chop_warning_at: Optional[datetime] = None
        self.last_missed_entry_alerts: Dict[str, datetime] = {}

    def latest_market_structure(self) -> Dict[str, Any]:
        path = LOG_DIR / "market_structure.jsonl"
        if not path.exists():
            return {}
        try:
            for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
                record = json.loads(line)
                if str(record.get("symbol") or "").upper() == "AAPL":
                    return record
        except (OSError, json.JSONDecodeError):
            return {}
        return {}

    def recent_liquidity_sweeps(self, now: datetime, minutes: int = 15) -> List[Dict[str, Any]]:
        path = LOG_DIR / "liquidity_sweeps.jsonl"
        if not path.exists():
            return []
        cutoff = now - timedelta(minutes=minutes)
        records: List[Dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
                record = json.loads(line)
                if str(record.get("symbol") or "").upper() != "AAPL":
                    continue
                timestamp = datetime.fromisoformat(str(record.get("timestamp")))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                if timestamp >= cutoff:
                    records.append(record)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return []
        return records

    def apply_market_structure_decision_quality(self, alert: Alert) -> None:
        structure_record = self.latest_market_structure()
        structure = structure_record.get("summary") if isinstance(structure_record.get("summary"), dict) else structure_record
        alert.market_structure_summary = structure.get("current_price_location_summary")
        alert.market_structure_warning = structure.get("structure_warning")
        alert.market_structure_range_low = structure.get("range_low")
        alert.market_structure_range_high = structure.get("range_high")
        warning = str(alert.market_structure_warning or "").lower()
        alert.market_structure_near_demand = "near demand" in warning
        alert.market_structure_near_supply = "near supply" in warning

        decision_config = self.config.get("decision_quality", {})
        sweep_records = self.recent_liquidity_sweeps(
            alert.timestamp,
            int(decision_config.get("chop_mode_lookback_minutes", 15)),
        )
        chop = evaluate_chop_mode(
            self.decision_history,
            structure_record,
            sweep_records,
            now=alert.timestamp,
            lookback_minutes=int(decision_config.get("chop_mode_lookback_minutes", 15)),
            min_flips=int(decision_config.get("chop_mode_min_flips", 2)),
            min_mixed_alerts=int(decision_config.get("chop_mode_min_mixed_alerts", 3)),
        )
        if decision_config.get("enable_chop_mode", True):
            alert.chop_mode_active = bool(chop["chop_mode_active"])
            alert.chop_mode_type = chop["chop_mode_type"] or None
            alert.chop_mode_reason = chop["chop_mode_reason"] or None
            alert.chop_suppression_active = bool(chop["suppression_active"])
            alert.chop_suppression_reason = chop["suppression_reason"] or None
            alert.sweep_risk_active = bool(chop.get("sweep_risk_active"))
            alert.upside_sweep_zone = chop.get("upside_sweep_zone")
            alert.downside_sweep_zone = chop.get("downside_sweep_zone")
            alert.recent_sweep_count = int(chop.get("recent_sweep_count") or 0)
            alert.sweep_risk_reason = chop.get("sweep_risk_reason") or None

        direction = str(alert.scenario_direction or alert.setup_direction or alert.direction or "").upper()
        setup_name = str(alert.setup_name or alert.primary_setup or (alert.scenario_top or {}).get("scenario_name") or "")
        stage = str(alert.scenario_stage or alert.setup_stage or (alert.scenario_top or {}).get("stage") or "").upper()
        latest_sweep = sweep_records[-1] if sweep_records else {}
        sweep_status = str(latest_sweep.get("sweep_status") or "").upper()
        sweep_direction = str(latest_sweep.get("sweep_direction") or "").upper()
        trap_bias = str(latest_sweep.get("trap_bias") or "NEUTRAL").upper()
        near_supply = bool(alert.market_structure_near_supply or (alert.sweep_risk_active and alert.upside_sweep_zone))
        near_demand = bool(alert.market_structure_near_demand or (alert.sweep_risk_active and alert.downside_sweep_zone))
        bullish_continuation = direction == "BULLISH" and any(
            token in setup_name.upper() for token in ("CONTINUATION", "BREAKOUT", "PULLBACK", "RECLAIM")
        )
        bearish_continuation = direction == "BEARISH" and any(
            token in setup_name.upper() for token in ("CONTINUATION", "BREAKDOWN", "PULLBACK", "REJECTION")
        )
        sweep_downgrade_reason = ""
        if bullish_continuation and near_supply and alert.sweep_risk_active:
            sweep_downgrade_reason = "Bullish chase is risky near supply; upside liquidity sweep risk active."
        elif bearish_continuation and near_demand and alert.sweep_risk_active:
            sweep_downgrade_reason = "Bearish chase is risky near demand; downside liquidity sweep risk active."
        if bullish_continuation and sweep_status == "SWEEP_CONFIRMED" and sweep_direction == "ABOVE_LEVEL":
            sweep_downgrade_reason = "Confirmed upside sweep: bullish continuation requires reclaim and hold above the swept level."
        elif bearish_continuation and sweep_status == "SWEEP_CONFIRMED" and sweep_direction == "BELOW_LEVEL":
            sweep_downgrade_reason = "Confirmed downside sweep: bearish continuation requires loss and hold below the swept level."
        if latest_sweep:
            alert.sweep_trap_bias = trap_bias
            alert.liquidity_sweep_context = str(latest_sweep.get("reason") or latest_sweep.get("meaning") or "")
        if sweep_downgrade_reason:
            alert.downgraded_by_liquidity_sweep = True
            alert.liquidity_sweep_downgrade_reason = sweep_downgrade_reason
            alert.sms_allowed = False
            alert.scenario_would_sms = False if alert.scenario_would_sms is not None else None
            alert.scenario_sms_allowed = False if alert.scenario_sms_allowed is not None else None
            if alert.risk_label != "DO_NOT_CHASE":
                alert.risk_label = "DO_NOT_CHASE"
            alert.entry_quality_label = "DO_NOT_CHASE"
            alert.do_not_chase_warning = True
            if sweep_downgrade_reason not in alert.strategy_warnings:
                alert.strategy_warnings.insert(0, sweep_downgrade_reason)
        if direction == "BEARISH" and ("PULLBACK" in setup_name.upper() or "REJECTION" in setup_name.upper()):
            reasons: List[str] = []
            vwap = alert.scenario_levels.get("vwap") if alert.scenario_levels else None
            ema9 = alert.scenario_levels.get("ema9") if alert.scenario_levels else None
            if isinstance(vwap, (int, float)) and alert.price > vwap:
                reasons.append("Bearish rejection is weak because price is still above VWAP.")
            if isinstance(ema9, (int, float)) and alert.price > ema9:
                reasons.append("Price is above EMA9.")
            if alert.volume_label == "WEAK":
                reasons.append("Volume is weak.")
            if alert.market_alignment == "OPPOSED":
                reasons.append("SPY/QQQ oppose the bearish setup.")
            if alert.market_structure_near_demand:
                reasons.append("Price is near demand.")
            if alert.chop_mode_active:
                reasons.append("Market structure is inside a chop range.")
            if reasons:
                alert.bearish_confirmation_quality = "WEAK"
                alert.bearish_confirmation_reason = " ".join(reasons)
                alert.bearish_downgraded_by_structure = bool(alert.market_structure_near_demand or alert.chop_mode_active)
                alert.bearish_downgrade_reason = " / ".join(reasons)
                alert.sms_allowed = False
                alert.scenario_would_sms = False if alert.scenario_would_sms is not None else None
                alert.scenario_sms_allowed = False if alert.scenario_sms_allowed is not None else None
                alert.watch_allowed = bool(alert.watch_allowed)
                if alert.bearish_confirmation_reason not in alert.strategy_warnings:
                    alert.strategy_warnings.insert(0, alert.bearish_confirmation_reason)
            else:
                alert.bearish_confirmation_quality = "STRONG"
                alert.bearish_confirmation_reason = "Below VWAP/EMA9 with no structure conflict detected."

        if decision_config.get("enable_missed_clean_entry_label", True):
            missed = detect_missed_clean_entry(
                self.decision_history,
                setup_name=setup_name,
                direction=direction,
                current_stage=stage,
                now=alert.timestamp,
                lookback_minutes=int(decision_config.get("missed_clean_entry_lookback_minutes", 15)),
            )
            if missed.get("missed_clean_entry"):
                key = f"{setup_name}|{direction}"
                last = self.last_missed_entry_alerts.get(key)
                cooldown = int(decision_config.get("missed_clean_entry_cooldown_minutes", 15))
                if not last or (alert.timestamp - last).total_seconds() >= cooldown * 60:
                    alert.missed_clean_entry = True
                    alert.previous_clean_setup_time = missed.get("previous_clean_setup_time")
                    alert.previous_clean_setup_name = missed.get("previous_clean_setup_name")
                    alert.previous_clean_setup_score = missed.get("previous_clean_setup_score")
                    alert.missed_clean_entry_reason = missed.get("missed_clean_entry_reason")
                    alert.lesson = missed.get("lesson")
                    self.last_missed_entry_alerts[key] = alert.timestamp

        clean_exit = clean_breakout_exits_chop(
            chop,
            price=alert.price,
            stage=stage,
            option_tradable=bool(alert.option_tradable),
            market_alignment=str(alert.market_alignment or ""),
            mixed_signal=bool(alert.mixed_signal_detected or alert.scenario_conflict),
            structure_warning=str(alert.market_structure_warning or ""),
        )
        if alert.chop_mode_active and not clean_exit and decision_config.get("chop_mode_suppress_repeated_alerts", True):
            cooldown = int(decision_config.get("chop_mode_cooldown_minutes", 15))
            warning_due = not self.last_chop_warning_at or (
                alert.timestamp - self.last_chop_warning_at
            ).total_seconds() >= cooldown * 60
            alert.sms_allowed = False
            alert.watch_allowed = False
            alert.phase3_heads_up_sent = bool(warning_due)
            alert.phase3_heads_up_eligible = bool(warning_due)
            alert.phase3_heads_up_type = "STOCK_ONLY_WARNING" if warning_due else "BLOCKED"
            alert.chop_warning_sent = bool(warning_due)
            alert.suppressed_by_chop = not warning_due
            if warning_due:
                self.last_chop_warning_at = alert.timestamp
            else:
                alert.phase3_heads_up_block_reason = "Suppressed: Chop Mode already warned inside cooldown"

        self.decision_history.append({
            "timestamp": alert.timestamp.isoformat(),
            "setup_name": setup_name,
            "direction": direction,
            "stage": stage,
            "score": alert.scenario_score or alert.setup_score,
            "phone_conclusion": alert.phone_conclusion,
            "decision_label": alert.decision_label,
        })
        cutoff = alert.timestamp - timedelta(minutes=max(30, int(decision_config.get("chop_mode_lookback_minutes", 15)) * 2))
        self.decision_history = [
            record for record in self.decision_history
            if datetime.fromisoformat(record["timestamp"]) >= cutoff
        ]

    def process_liquidity_sweep_telegram(self, snap: SymbolSnapshot) -> bool:
        settings = self.config.get("liquidity_sweep_engine", {})
        notifications = self.config.get("notifications", {})
        if not settings.get("enabled", True) or not settings.get("telegram_enabled", True):
            return False
        try:
            from tools.preview_liquidity_sweeps import build_liquidity_sweep_preview

            payload = build_liquidity_sweep_preview(
                snap.symbol,
                snap.recent_bars,
                daily_bars=snap.daily_bars,
                config=self.config,
            )
            if not settings.get("telegram_include_structure", True):
                payload.pop("market_structure_summary", None)
        except Exception as exc:
            logger.warning("Liquidity sweep Telegram evaluation failed safely: %s", redact_notification_error(exc))
            return False

        eligible, reason, alert_type = sweep_telegram_eligibility(payload, self.config)
        log_fields: Dict[str, Any] = {
            "telegram_eligible": bool(eligible),
            "telegram_sent": False,
            "telegram_alert_type": alert_type,
            "telegram_suppressed_reason": "" if eligible else reason,
            "openai_formatter_used": False,
            "openai_validation_passed": False,
            "fallback_used": False,
        }
        if not eligible or not alert_type:
            append_sweep_telegram_log(LOG_DIR / "liquidity_sweeps.jsonl", payload, **log_fields)
            return False
        if not notifications.get("telegram_enabled", False):
            log_fields["telegram_suppressed_reason"] = "global Telegram notifications disabled"
            append_sweep_telegram_log(LOG_DIR / "liquidity_sweeps.jsonl", payload, **log_fields)
            return False

        max_chars = int(settings.get("telegram_max_chars", 900))
        rule_message, formatter_fields = select_liquidity_sweep_message(payload, max_chars=max_chars)
        log_fields.update(formatter_fields)
        valid, validation_reason = validate_liquidity_sweep_message(
            payload,
            rule_message,
            rule_message=rule_message,
            max_chars=max_chars,
        )
        if not valid:
            log_fields["telegram_suppressed_reason"] = f"deterministic sweep message validation failed: {validation_reason}"
            log_fields["fallback_used"] = True
            append_sweep_telegram_log(LOG_DIR / "liquidity_sweeps.jsonl", payload, **log_fields)
            return False

        allowed, dedupe_reason, _ = claim_sweep_delivery(
            payload,
            int(settings.get("telegram_cooldown_minutes", 10)),
            LIQUIDITY_SWEEP_TELEGRAM_DEDUPE_STATE,
        )
        if not allowed:
            log_fields["telegram_suppressed_reason"] = dedupe_reason
            append_sweep_telegram_log(LOG_DIR / "liquidity_sweeps.jsonl", payload, **log_fields)
            return False

        sent, error = send_telegram_message(
            token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            message=rule_message,
            timeout_seconds=int(notifications.get("telegram_timeout_seconds", 8)),
            alert_type=alert_type,
            alert_source="LIQUIDITY_SWEEP_ENGINE",
            symbol="AAPL",
            sms_sent=False,
            alert_tier="CONTEXT",
            alert_tier_reason="Liquidity sweep context cannot approve trades",
            message_source_path="scanner.liquidity_sweep_telegram",
            phone_conclusion="WATCH ONLY",
            phone_conclusion_reason=str(payload.get("reason") or "Liquidity sweep context"),
        )
        log_fields["telegram_sent"] = bool(sent)
        log_fields["telegram_suppressed_reason"] = "" if sent else error
        append_sweep_telegram_log(LOG_DIR / "liquidity_sweeps.jsonl", payload, **log_fields)
        return bool(sent)

    def build_snapshots(self) -> Dict[str, SymbolSnapshot]:
        end = now_utc()
        start = session_history_start(self.config)
        latest = self.provider.get_latest_bars(self.data_symbols)
        recent: Dict[str, List[Bar]] = {}
        minutes_requested = max(1, int((end - start).total_seconds() // 60) + 1)
        api_safe_batch_size = max(1, 9000 // minutes_requested)
        configured_batch_size = max(1, int(self.config.get("discovery", {}).get("batch_size", 150)))
        batch_size = min(configured_batch_size, api_safe_batch_size)
        for i in range(0, len(self.data_symbols), batch_size):
            batch_symbols = self.data_symbols[i:i + batch_size]
            recent.update(self.provider.get_recent_bars(batch_symbols, start, end))
        daily: Dict[str, List[Bar]] = {}
        if hasattr(self.provider, "get_daily_bars"):
            try:
                daily = self.provider.get_daily_bars(self.data_symbols, end - timedelta(days=45), end)
            except Exception as exc:
                logger.warning("Daily structure bars unavailable: %s", exc)
        news_config = self.config.get("news_context", {})
        news_enabled = bool(news_config.get("enabled", False))
        news_watch_symbols = [
            str(symbol).upper()
            for symbol in news_config.get("watch_symbols", ["AAPL"])
            if str(symbol).upper() in self.symbols
        ]
        news: List[NewsItem] = []
        if news_enabled and news_watch_symbols:
            try:
                news = self.provider.get_news(news_watch_symbols)
            except Exception as exc:
                logger.warning("News context unavailable: %s", exc)

        latest_news_map: Dict[str, NewsItem] = {}
        max_age = timedelta(minutes=int(news_config.get("lookback_minutes", self.config["max_news_age_minutes"])))
        cutoff = now_utc() - max_age
        for item in news:
            if item.published_at >= cutoff and item.symbol not in latest_news_map:
                latest_news_map[item.symbol] = item

        snapshots: Dict[str, SymbolSnapshot] = {}
        for symbol in self.data_symbols:
            rbars = recent.get(symbol, [])
            latest_bar = latest.get(symbol)
            pm_high = None
            pm_low = None
            or_high = None
            or_low = None
            or_15_high = None
            or_15_low = None

            pre_bars = [b for b in rbars if is_premarket_bar(b.t, self.config)]
            if pre_bars:
                pm_high = max(b.h for b in pre_bars)
                pm_low = min(b.l for b in pre_bars)

            or_bars = [b for b in rbars if is_opening_range_bar(b.t, self.config)]
            if or_bars:
                or_high = max(b.h for b in or_bars)
                or_low = min(b.l for b in or_bars)
            secondary_minutes = int(
                self.config.get("strategy_engine", {}).get(
                    "opening_range_minutes_secondary",
                    self.config.get("opening_range_minutes_secondary", 15),
                )
            )
            or_15_bars = [b for b in rbars if is_opening_range_bar_for_minutes(b.t, self.config, secondary_minutes)]
            if or_15_bars:
                or_15_high = max(b.h for b in or_15_bars)
                or_15_low = min(b.l for b in or_15_bars)
            completed_daily = [
                bar for bar in daily.get(symbol, [])
                if bar.t.astimezone(ET).date() < now_et().date()
            ]
            structure_bars = list(rbars)
            if latest_bar and (not structure_bars or latest_bar.t > structure_bars[-1].t):
                structure_bars.append(latest_bar)
            multi_timeframe_context = evaluate_multi_timeframe_context(
                structure_bars,
                daily_bars=completed_daily,
                premarket_high=pm_high,
                premarket_low=pm_low,
            )

            option_chain: List[OptionContractSnapshot] = []
            if symbol in self.symbols and self.config.get("options", {}).get("enabled", True) and latest_bar:
                try:
                    option_chain = self.provider.get_option_chain(symbol, self.config)
                except Exception as exc:
                    logger.warning("Option chain unavailable for %s: %s", symbol, exc)
            best_call, best_put = select_option_contracts(
                option_chain,
                latest_bar.c if latest_bar else None,
                self.config,
            )

            snapshots[symbol] = SymbolSnapshot(
                symbol=symbol,
                latest_bar=latest_bar,
                recent_bars=rbars,
                premarket_high=pm_high,
                premarket_low=pm_low,
                opening_range_high=or_high,
                opening_range_low=or_low,
                opening_range_15_high=or_15_high,
                opening_range_15_low=or_15_low,
                daily_bars=completed_daily,
                multi_timeframe_context=multi_timeframe_context,
                latest_news=latest_news_map.get(symbol),
                best_call=best_call,
                best_put=best_put,
            )
        return snapshots

    def compute_relative_volume(self, bars: List[Bar]) -> Optional[float]:
        if len(bars) < 10:
            return None
        recent_vol = bars[-1].v
        prior_avg = average(b.v for b in bars[:-1])
        if not prior_avg or prior_avg <= 0:
            return None
        return recent_vol / prior_avg

    def passes_basic_filters(self, snap: SymbolSnapshot) -> bool:
        if not snap.latest_bar or not snap.recent_bars:
            return False
        price = snap.latest_bar.c
        filters = self.config["filters"]
        if price < filters["min_price"] or price > filters["max_price"]:
            return False
        day_volume = sum(b.v for b in snap.recent_bars)
        if day_volume < filters["min_day_volume"]:
            return False
        return True

    def infer_alert_direction(self, alert: Alert) -> str:
        category = alert.category.upper()
        bullish_terms = ("BREAK UP", "HIGH BREAK", "REVERSAL UP", "SUSTAINED TREND UP", "SQUEEZE")
        bearish_terms = ("BREAK DOWN", "LOW BREAK", "REVERSAL DOWN", "FAILED BREAKOUT DOWN", "SUSTAINED TREND DOWN", "FLUSH", "SELL")
        if any(term in category for term in bullish_terms):
            return "BULLISH"
        if any(term in category for term in bearish_terms):
            return "BEARISH"
        move = alert.fast_move_pct
        if move is None or abs(move) < 0.08:
            move = alert.day_move_pct
        if move is not None and move > 0:
            return "BULLISH"
        if move is not None and move < 0:
            return "BEARISH"
        return "NEUTRAL"

    def is_watch_alert(self, alert: Alert) -> bool:
        return (alert.setup_level or "").upper() == "WATCH" or alert.category.upper().startswith("WATCH ")

    def build_market_context(self, snapshots: Dict[str, SymbolSnapshot]) -> Dict[str, str]:
        context: Dict[str, str] = {}
        for symbol in self.market_context_symbols():
            snap = snapshots.get(symbol)
            if not snap or snapshot_data_quality(snap, self.config) != "Fresh" or not snap.latest_bar or not snap.recent_bars:
                context[symbol] = "UNKNOWN"
                continue
            anchor = session_anchor_bar(snap.recent_bars, self.config)
            lookback = int(self.config["lookback_minutes_fast_move"])
            fast = 0.0
            if len(snap.recent_bars) > lookback:
                fast = pct_change(snap.latest_bar.c, snap.recent_bars[-(lookback + 1)].c)
            day = pct_change(snap.latest_bar.c, anchor.o) if anchor else 0.0
            combined = day if abs(day) >= 0.08 else fast
            if combined > 0.05:
                context[symbol] = "BULLISH"
            elif combined < -0.05:
                context[symbol] = "BEARISH"
            else:
                context[symbol] = "FLAT"
        return context

    def market_alignment_for(self, direction: str, market_context: Optional[Dict[str, str]]) -> str:
        if direction not in {"BULLISH", "BEARISH"} or not market_context:
            return "UNKNOWN"
        reads = [market_context.get(symbol, "UNKNOWN") for symbol in self.market_context_symbols()]
        if any(read == "UNKNOWN" for read in reads):
            return "UNKNOWN"
        opposed = "BEARISH" if direction == "BULLISH" else "BULLISH"
        if all(read == direction for read in reads):
            return "ALIGNED"
        if all(read == opposed for read in reads):
            return "OPPOSED"
        return "MIXED"

    def breakout_hold_ok(self, alert: Alert, snap: SymbolSnapshot) -> bool:
        category = alert.category.upper()
        required = int(self.config.get("alert_quality", {}).get("hold_break_bars", 1))
        if required <= 0:
            return True
        if "BREAK" not in category:
            return True
        level: Optional[float] = None
        bullish = False
        if "OPENING RANGE BREAK UP" in category:
            level = snap.opening_range_high
            bullish = True
        elif "OPENING RANGE BREAK DOWN" in category:
            level = snap.opening_range_low
        elif "PREMARKET HIGH BREAK" in category:
            level = snap.premarket_high
            bullish = True
        elif "PREMARKET LOW BREAK" in category:
            level = snap.premarket_low
        else:
            return True
        if level is None or len(snap.recent_bars) < required + 1:
            return False
        check_bars = snap.recent_bars[-(required + 1):]
        if bullish:
            return all(bar.c > level for bar in check_bars)
        return all(bar.c < level for bar in check_bars)

    def breakout_distance_pct(self, alert: Alert, snap: SymbolSnapshot) -> Optional[float]:
        category = alert.category.upper()
        level: Optional[float] = None
        bullish = False
        if "OPENING RANGE BREAK UP" in category:
            level = snap.opening_range_high
            bullish = True
        elif "OPENING RANGE BREAK DOWN" in category:
            level = snap.opening_range_low
        elif "PREMARKET HIGH BREAK" in category:
            level = snap.premarket_high
            bullish = True
        elif "PREMARKET LOW BREAK" in category:
            level = snap.premarket_low
        else:
            return None
        if level is None or level <= 0:
            return None
        raw = pct_change(alert.price, level)
        return raw if bullish else -raw

    def immediate_break_ok(self, alert: Alert, snap: SymbolSnapshot) -> bool:
        if "BREAK" not in alert.category.upper():
            return False
        quality_config = self.config.get("alert_quality", {})
        distance = self.breakout_distance_pct(alert, snap)
        if distance is None:
            return False
        min_distance = float(quality_config.get("immediate_break_min_distance_pct", 0.20))
        min_fast = float(quality_config.get("immediate_break_min_fast_move_pct", 0.20))
        direction = alert.direction or self.infer_alert_direction(alert)
        fast_move = alert.fast_move_pct or 0.0
        fast_aligned = fast_move >= min_fast if direction == "BULLISH" else fast_move <= -min_fast
        return distance >= min_distance and fast_aligned

    def max_allowed_break_distance_pct(self, alert: Alert) -> float:
        quality_config = self.config.get("alert_quality", {})
        if alert.symbol in {"SPY", "QQQ"}:
            return float(quality_config.get("max_sms_index_break_distance_pct", 0.30))
        return float(quality_config.get("max_sms_break_distance_pct", 0.75))

    def late_day_threshold_time(self) -> datetime:
        quality_config = self.config.get("alert_quality", {})
        raw = str(quality_config.get("late_day_repeat_after", "14:30"))
        hour, minute = (int(part) for part in raw.split(":", 1))
        now = now_et()
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def is_late_day_generic_alert(self, alert: Alert) -> bool:
        if alert.timestamp.astimezone(ET) < self.late_day_threshold_time():
            return False
        category = alert.category.upper()
        return "BREAK" not in category and ("HIGH RELATIVE VOLUME" in category or "CATALYST RUNNER" in category)

    def fast_move_aligned(self, alert: Alert, minimum: float) -> bool:
        fast = alert.fast_move_pct or 0.0
        direction = alert.direction or self.infer_alert_direction(alert)
        if direction == "BULLISH":
            return fast >= minimum
        if direction == "BEARISH":
            return fast <= -minimum
        return False

    def fast_move_opposes_setup(self, alert: Alert, minimum: float) -> bool:
        fast = alert.fast_move_pct or 0.0
        direction = alert.direction or self.infer_alert_direction(alert)
        if direction == "BULLISH":
            return fast <= -minimum
        if direction == "BEARISH":
            return fast >= minimum
        return False

    def market_opposed_bearish_watch_allowed(
        self,
        alert: Alert,
        data_quality: str,
        age: Optional[float],
        max_age: float,
        score: int,
        rvol: float,
        fast_opposes_setup: bool,
        break_distance: Optional[float],
    ) -> bool:
        quality_config = self.config.get("alert_quality", {})
        if not quality_config.get("opposed_bearish_watch_enabled", True):
            return False
        if (alert.direction or self.infer_alert_direction(alert)) != "BEARISH":
            return False
        if alert.market_alignment != "OPPOSED":
            return False
        if data_quality != "Fresh" or age is None or age > max_age:
            return False
        if score < int(quality_config.get("opposed_bearish_watch_min_score", 15)):
            return False
        if rvol < float(quality_config.get("opposed_bearish_watch_min_rvol", 0.30)):
            return False
        if fast_opposes_setup:
            return False
        if not option_quality_is_tradable(alert.option_quality):
            return False
        if (alert.options_score or 0) < int(quality_config.get("opposed_bearish_watch_min_options_score", 65)):
            return False
        if alert.option_spread_pct is not None and alert.option_spread_pct > float(quality_config.get("max_sms_option_spread_pct", 8.0)):
            return False

        if break_distance is not None and break_distance > float(quality_config.get("opposed_bearish_watch_max_break_distance_pct", 1.50)):
            return False

        fast = alert.fast_move_pct or 0.0
        day = alert.day_move_pct or 0.0
        min_fast = float(quality_config.get("opposed_bearish_watch_min_fast_move_pct", 0.08))
        min_day = float(quality_config.get("opposed_bearish_watch_min_day_move_pct", 0.75))
        max_counter_day = float(quality_config.get("opposed_bearish_watch_max_counter_day_move_pct", 2.5))
        if day > max_counter_day:
            return False
        category = alert.category.upper()
        bearish_watch_category = any(
            term in category
            for term in (
                "BREAK DOWN",
                "LOW BREAK",
                "REVERSAL DOWN",
                "FAILED BREAKOUT DOWN",
                "SUSTAINED TREND DOWN",
                "FAST IMPULSE DOWN",
            )
        )
        stock_move_confirms = fast <= -min_fast or day <= -min_day
        return bearish_watch_category and stock_move_confirms

    def maybe_fast_impulse_watch(
        self,
        snap: SymbolSnapshot,
        latest: Bar,
        fast_move: float,
        day_move: float,
        rel_vol: Optional[float],
        notes: List[str],
    ) -> Optional[Alert]:
        quality_config = self.config.get("alert_quality", {})
        if not quality_config.get("fast_impulse_watch_enabled", True):
            return None
        latest_et = latest.t.astimezone(ET)
        open_t = set_today_time_et(self.config["market_open"])
        close_t = set_today_time_et(self.config["market_close"])
        if not (open_t <= latest_et < close_t):
            return None
        min_rvol = float(quality_config.get("fast_impulse_watch_min_rvol", 1.2))
        if (rel_vol or 0.0) < min_rvol:
            return None

        lookback = max(2, int(quality_config.get("fast_impulse_watch_lookback_bars", 3)))
        if len(snap.recent_bars) < lookback + 1:
            return None
        window = snap.recent_bars[-(lookback + 1):]
        impulse_move = pct_change(latest.c, window[0].c)
        min_move = float(quality_config.get("fast_impulse_watch_min_move_pct", 0.18))
        if snap.symbol in {"SPY", "QQQ"}:
            min_move = float(quality_config.get("fast_impulse_watch_min_index_move_pct", min_move))

        if impulse_move >= min_move:
            direction = "BULLISH"
        elif impulse_move <= -min_move:
            direction = "BEARISH"
        else:
            return None

        aligned_steps = 0
        for previous, current in zip(window, window[1:]):
            if direction == "BULLISH" and current.c >= previous.c:
                aligned_steps += 1
            elif direction == "BEARISH" and current.c <= previous.c:
                aligned_steps += 1
        min_ratio = float(quality_config.get("fast_impulse_watch_min_aligned_ratio", 0.66))
        if aligned_steps / lookback < min_ratio:
            return None

        if direction == "BULLISH" and latest.c < latest.o:
            return None
        if direction == "BEARISH" and latest.c > latest.o:
            return None

        category = "WATCH FAST IMPULSE UP" if direction == "BULLISH" else "WATCH FAST IMPULSE DOWN"
        return Alert(
            symbol=snap.symbol,
            timestamp=now_utc(),
            category=category,
            price=latest.c,
            fast_move_pct=fast_move,
            day_move_pct=day_move,
            relative_volume=rel_vol,
            premarket_high=snap.premarket_high,
            premarket_low=snap.premarket_low,
            opening_range_high=snap.opening_range_high,
            opening_range_low=snap.opening_range_low,
            headline=snap.latest_news.headline if snap.latest_news else None,
            url=snap.latest_news.url if snap.latest_news else None,
            notes=notes + [f"fast {lookback}m impulse move {impulse_move:+.2f}%"],
            setup_level="WATCH",
        )

    def late_day_generic_has_fresh_push(self, alert: Alert) -> bool:
        quality_config = self.config.get("alert_quality", {})
        min_fast = float(quality_config.get("late_day_generic_min_aligned_fast_move_pct", 0.12))
        min_rvol = float(quality_config.get("late_day_generic_min_rvol", 3.5))
        return self.fast_move_aligned(alert, min_fast) and (alert.relative_volume or 0.0) >= min_rvol

    def phase2_active_strategy_directions(self, alert: Alert) -> List[str]:
        directions: List[str] = []
        for result in alert.strategy_results:
            if not result.get("active"):
                continue
            direction = str(result.get("direction") or "").upper()
            if direction in {"BULLISH", "BEARISH"} and direction not in directions:
                directions.append(direction)
        return directions

    def has_phase2_context(self, alert: Alert) -> bool:
        return bool(
            alert.primary_setup
            or alert.strategy_results
            or alert.confirmation_score is not None
            or alert.candle_label
            or alert.risk_label
            or alert.market_regime
        )

    def phase2_direction_conflict(self, alert: Alert, alert_direction: str) -> bool:
        if not alert_direction:
            return False
        if alert.strategy_direction in {"bullish", "bearish"} and alert.strategy_direction.upper() != alert_direction:
            return True
        active_directions = self.phase2_active_strategy_directions(alert)
        opposite = "BEARISH" if alert_direction == "BULLISH" else "BULLISH"
        for result in alert.strategy_results:
            direction = str(result.get("direction") or "").upper()
            if result.get("active") and direction == opposite and int(result.get("score") or 0) >= 60:
                return True
        return bool(active_directions and alert_direction not in active_directions)

    def phase2_candle_aligned(self, alert: Alert, alert_direction: str) -> bool:
        label = alert.candle_label or "UNKNOWN"
        if alert_direction == "BULLISH":
            return label == "BUYER_CONTROL"
        if alert_direction == "BEARISH":
            return label == "SELLER_CONTROL"
        return False

    def phase2_candle_contradicts(self, alert: Alert, alert_direction: str) -> bool:
        label = alert.candle_label or "UNKNOWN"
        return (
            (alert_direction == "BULLISH" and label == "SELLER_CONTROL")
            or (alert_direction == "BEARISH" and label == "BUYER_CONTROL")
        )

    def market_regime_supports_direction(self, alert: Alert, alert_direction: str) -> bool:
        regime = alert.market_regime or "UNKNOWN"
        if alert_direction == "BULLISH":
            return regime in {"TRENDING_UP", "OPENING_DRIVE_UP", "BULL_TREND"}
        if alert_direction == "BEARISH":
            return regime in {"TRENDING_DOWN", "OPENING_DRIVE_DOWN", "BEAR_TREND"}
        return False

    def phase2_sms_block_reasons(self, alert: Alert, alert_direction: str) -> List[str]:
        if not self.has_phase2_context(alert):
            return []
        quality_config = self.config.get("alert_quality", {})
        reasons: List[str] = []
        min_confirmation = int(quality_config.get("sms_min_confirmation_score", 60))
        strong_confirmation = int(quality_config.get("sms_strong_confirmation_score", 70))
        direction_conflict = self.phase2_direction_conflict(alert, alert_direction)
        if bool(quality_config.get("sms_require_no_direction_conflict", True)) and direction_conflict:
            reasons.append("Direction conflict: setup label does not match alert direction")
        if alert.risk_label in {"HIGH", "DO_NOT_CHASE"}:
            reasons.append(f"Phase 2 risk is {alert.risk_label}")
        if alert.confirmation_score is not None and alert.confirmation_score < min_confirmation:
            reasons.append("Phase 2 confirmation below SMS threshold")
        if alert.confirmation_label == "WEAK":
            reasons.append("Phase 2 confirmation label is WEAK")
        if bool(quality_config.get("sms_require_candle_alignment", True)):
            if alert.candle_label in {"INDECISION", "REJECTION"}:
                reasons.append(f"Candle quality is {alert.candle_label}")
            elif self.phase2_candle_contradicts(alert, alert_direction):
                reasons.append("Candle quality contradicts alert direction")
            elif alert.candle_label and not self.phase2_candle_aligned(alert, alert_direction):
                reasons.append("Candle quality is not aligned with alert direction")
        market_regime = alert.market_regime or "UNKNOWN"
        if bool(quality_config.get("sms_block_choppy_market", True)) and market_regime in {"CHOPPY", "UNKNOWN"}:
            if not ((alert.strategy_confidence_score or 0) >= 90 and (alert.confirmation_score or 0) >= strong_confirmation):
                reasons.append(f"Market regime is {market_regime}")
        if alert.relative_strength_label == "UNKNOWN" and (alert.confirmation_score or 0) < strong_confirmation:
            reasons.append("Relative strength is UNKNOWN below strong confirmation threshold")
        return reasons

    def phase2_grade_cap(self, alert: Alert, alert_direction: str) -> int:
        if not self.has_phase2_context(alert):
            return 100
        cap = 100
        direction_conflict = self.phase2_direction_conflict(alert, alert_direction)
        candle_aligned = self.phase2_candle_aligned(alert, alert_direction)
        if direction_conflict:
            cap = min(cap, 54)
        if alert.confirmation_score is not None and alert.confirmation_score < int(self.config.get("alert_quality", {}).get("sms_min_confirmation_score", 60)):
            cap = min(cap, 69)
        if alert.confirmation_label != "STRONG":
            cap = min(cap, 69)
        if alert.risk_label in {"HIGH", "DO_NOT_CHASE"}:
            cap = min(cap, 69)
        if (alert.market_regime or "UNKNOWN") in {"CHOPPY", "UNKNOWN"}:
            cap = min(cap, 69)
        if not candle_aligned:
            cap = min(cap, 69)
        return cap

    def phase2_allows_a_plus(self, alert: Alert, alert_direction: str) -> bool:
        if not self.has_phase2_context(alert):
            return True
        quality_config = self.config.get("alert_quality", {})
        if self.phase2_direction_conflict(alert, alert_direction):
            return False
        if (alert.strategy_confidence_score or 0) < 85:
            return False
        if (alert.confirmation_score or 0) < int(quality_config.get("a_plus_min_confirmation_score", 70)):
            return False
        if alert.risk_label not in {"LOW", "MEDIUM"}:
            return False
        if alert.entry_quality_label not in {"GOOD_POSITION", "EARLY"}:
            return False
        if alert.volume_label not in {"STRONG", "CLIMAX"}:
            return False
        if not self.phase2_candle_aligned(alert, alert_direction):
            return False
        if not self.market_regime_supports_direction(alert, alert_direction):
            return False
        if any("direction conflict" in warning.lower() for warning in alert.strategy_warnings):
            return False
        return True

    def is_orb_sms_alert(self, alert: Alert) -> bool:
        text = " ".join([alert.category or "", alert.primary_setup or "", *alert.secondary_setups]).upper()
        return "ORB" in text or "OPENING RANGE" in text

    def orb_sms_state_key(self, alert: Alert) -> str:
        direction = alert.direction or self.infer_alert_direction(alert) or "MOMENTUM"
        day_key = alert.timestamp.astimezone(ET).strftime("%Y-%m-%d")
        return f"{day_key}:ORB_SMS:{alert.symbol}:{direction}"

    def orb_sms_blocked_by_dedupe(self, alert: Alert) -> bool:
        if not self.is_orb_sms_alert(alert):
            return False
        minutes = int(self.config.get("alert_quality", {}).get("sms_orb_dedupe_minutes", 15))
        key = self.orb_sms_state_key(alert)
        last = self.state_store.get_last_alert_time(key)
        if not last:
            return False
        if (now_utc() - last).total_seconds() > minutes * 60:
            return False
        previous = int(self.state_store.data.get("orb_sms_confirmation_scores", {}).get(key, -1))
        return (alert.confirmation_score or 0) <= previous

    def record_orb_sms_state(self, alert: Alert) -> None:
        if not self.is_orb_sms_alert(alert):
            return
        key = self.orb_sms_state_key(alert)
        self.state_store.set_last_alert_time(key, now_utc())
        self.state_store.data.setdefault("orb_sms_confirmation_scores", {})[key] = int(alert.confirmation_score or 0)

    def phase3_heads_up_symbols(self) -> set[str]:
        raw = self.config.get("scenario_engine", {}).get("phase3_heads_up_symbols", ["AAPL"])
        if isinstance(raw, str):
            values = [part.strip().upper() for part in raw.split(",")]
        elif isinstance(raw, list):
            values = [str(part).strip().upper() for part in raw]
        else:
            values = []
        return {value for value in values if value}

    def market_context_symbols(self) -> List[str]:
        raw = self.config.get("scenario_engine", {}).get("market_context_symbols", ["SPY", "QQQ"])
        if isinstance(raw, str):
            values = [part.strip().upper() for part in raw.split(",")]
        elif isinstance(raw, list):
            values = [str(part).strip().upper() for part in raw]
        else:
            values = []
        return [value for value in values if value]

    def phase3_heads_up_state_key(self, alert: Alert) -> str:
        day_key = alert.timestamp.astimezone(ET).strftime("%Y-%m-%d")
        return f"{day_key}:{phase3_heads_up_dedupe_key(alert)}"

    def evaluate_phase3_heads_up(
        self,
        alert: Alert,
        snap: SymbolSnapshot,
        data_quality: Optional[str] = None,
        market_context: Optional[Dict[str, str]] = None,
    ) -> None:
        config = self.config.get("scenario_engine", {})
        alert.phase3_heads_up_eligible = False
        alert.phase3_heads_up_sent = False
        alert.phase3_heads_up_block_reason = ""
        alert.phase3_heads_up_type = "BLOCKED"
        alert.phase3_heads_up_dedupe_key = None
        alert.phase3_heads_up_message_fingerprint = None
        alert.phase3_heads_up_dedupe_blocked = False
        alert.phase3_heads_up_dedupe_reason = None
        alert.phase3_heads_up_last_sent_time = None
        alert.phase3_heads_up_next_eligible_time = None
        alert.phase3_heads_up_dedupe_minutes_remaining = None
        context_symbols = self.market_context_symbols()
        alert.context_symbols_expected = list(context_symbols)
        alert.context_symbols_available = [
            symbol for symbol in context_symbols if (market_context or {}).get(symbol, "UNKNOWN") != "UNKNOWN"
        ]
        alert.market_confirmation_status = (
            "AVAILABLE" if len(alert.context_symbols_available) == len(context_symbols) else "UNAVAILABLE"
        )
        if alert.market_confirmation_status == "UNAVAILABLE":
            alert.market_context_missing_warning = True
            warning = "Market confirmation unavailable — check SPY/QQQ manually."
            if warning not in alert.scenario_warnings:
                alert.scenario_warnings.append(warning)
        else:
            alert.market_context_missing_warning = False
        alert.stock_only_heads_up_allowed = False
        alert.stock_only_heads_up_reason = ""
        alert.phase3_heads_up_final_decision = "BLOCKED"
        alert.phase3_heads_up_final_block_reason = ""
        alert.option_stale_did_not_block_heads_up = False
        alert.watch_only_late_move = False
        alert.do_not_chase_watch = False

        def block(reason: str) -> None:
            alert.phase3_heads_up_block_reason = reason
            alert.phase3_heads_up_final_block_reason = reason
            alert.phase3_heads_up_message_preview = phase3_heads_up_message(alert)

        if not config.get("enable_phase3_heads_up_alerts", True):
            block("Phase 3 heads-up alerts disabled")
            return
        if alert.symbol.upper() not in self.phase3_heads_up_symbols():
            block("symbol not enabled for Phase 3 heads-up")
            return
        if not alert.scenario_top:
            block("no Phase 3 top scenario")
            return

        scenario = alert.scenario_top
        scenario_name = str(scenario.get("scenario_name") or "").strip()
        scenario_direction = str(alert.scenario_direction or scenario.get("direction") or "").upper()
        if (data_quality or snapshot_data_quality(snap, self.config)) != "Fresh":
            block("Blocked: stale data")
            return

        stage = str(alert.scenario_stage or scenario.get("stage") or "").upper()
        min_scenario = int(config.get("phase3_heads_up_min_scenario_score", 80))
        min_stock = int(config.get("phase3_heads_up_min_stock_score", 65))
        min_confirmation = int(config.get("phase3_heads_up_min_confirmation_score", 55))
        scenario_score = int(alert.scenario_score or scenario.get("score") or 0)
        stock_score = int(alert.stock_setup_score or alert.strategy_confidence_score or 0)
        confirmation_score = int(alert.confirmation_score or 0)
        risk = str(alert.risk_label or alert.scenario_risk_label or "").upper()
        entry_quality = str(alert.entry_quality_label or "").upper()
        available_context = set(alert.context_symbols_available)
        has_spy_qqq = {"SPY", "QQQ"}.issubset(available_context)
        context_aligned = str(alert.market_alignment or "").upper() == "ALIGNED"
        context_ready = alert.market_confirmation_status == "AVAILABLE" or context_aligned
        extended = (
            str(alert.extension_label or "").upper() in {"EXTENDED", "VERY_EXTENDED", "DO_NOT_CHASE"}
            or bool(scenario.get("do_not_chase"))
            or any(
                "extended" in str(item).lower()
                for item in list(alert.scenario_reasons or []) + list(alert.scenario_warnings or [])
            )
        )
        high_or_do_not_chase = "HIGH" in risk or "DO_NOT_CHASE" in risk
        allowed_scenarios = {
            "Pullback Holding",
            "Pullback Rejecting",
            "Bullish VWAP/EMA Reclaim Continuation",
            "Bearish VWAP/EMA Rejection Continuation",
            "Bullish Trend Continuation",
            "Bearish Trend Continuation",
            "Breakout Continuation",
            "Failed VWAP/EMA Reclaim",
        }
        watch_only_late_move = (
            alert.symbol.upper() == "AAPL"
            and bool(phase3_heads_up_message(alert))
            and (not alert.scenario_conflict or bool(alert.mixed_signal_reason))
            and (context_ready or bool(alert.market_context_missing_warning))
            and (has_spy_qqq or bool(alert.market_context_missing_warning))
            and scenario_direction in {"BULLISH", "BEARISH"}
            and scenario_score >= 80
            and confirmation_score >= 50
            and stock_score >= 50
            and stage in {"LATE", "DO_NOT_CHASE"}
            and scenario_name in allowed_scenarios
        )
        do_not_chase_watch = (
            alert.symbol.upper() == "AAPL"
            and bool(phase3_heads_up_message(alert))
            and scenario_name == "Do Not Chase"
            and scenario_score >= 55
            and (context_aligned or has_spy_qqq)
            and extended
        )
        special_watch_only = watch_only_late_move or do_not_chase_watch

        if not special_watch_only and scenario_name not in allowed_scenarios:
            block(f"scenario {scenario_name or 'unknown'} is not heads-up eligible")
            return
        if not special_watch_only and scenario_direction not in {"BULLISH", "BEARISH"}:
            block("top scenario has no bullish/bearish direction")
            return

        failure_or_rejection = any(term in scenario_name.lower() for term in ("reject", "fail"))
        candle_opposes = self.phase2_candle_contradicts(alert, scenario_direction)
        stock_only_allowed = (
            alert.symbol.upper() == "AAPL"
            and scenario_score >= 75
            and confirmation_score >= 55
            and risk != "HIGH"
            and (not alert.scenario_conflict or failure_or_rejection)
            and (not candle_opposes or failure_or_rejection)
            and stage not in {"INVALIDATED"}
        )
        if watch_only_late_move:
            alert.watch_only_late_move = True
            alert.stock_only_heads_up_allowed = True
            alert.stock_only_heads_up_reason = (
                "WATCH_ONLY_LATE_MOVE: aligned AAPL late/high-risk movement met watch thresholds"
            )
            alert.phase3_heads_up_type = "WATCH_ONLY_LATE_MOVE"
        elif do_not_chase_watch:
            alert.do_not_chase_watch = True
            alert.stock_only_heads_up_allowed = True
            alert.stock_only_heads_up_reason = (
                "DO_NOT_CHASE_WATCH: extended AAPL move met watch-only threshold"
            )
            alert.phase3_heads_up_type = "DO_NOT_CHASE_WATCH"

        normal_block_reason = ""
        if stage in {"LATE", "DO_NOT_CHASE", "INVALIDATED"}:
            normal_block_reason = f"Blocked: scenario stage is {stage}"
        elif stage not in {"FORMING", "CONFIRMED", "GOOD_POSITION"}:
            normal_block_reason = f"scenario stage is {stage or 'UNKNOWN'}"
        elif scenario_score < min_scenario:
            normal_block_reason = "scenario score below heads-up threshold"
        elif stock_score < min_stock:
            normal_block_reason = "stock setup score below heads-up threshold"
        elif confirmation_score < min_confirmation:
            normal_block_reason = f"Blocked: confirmation below {min_confirmation}"
        elif risk in {"HIGH", "DO_NOT_CHASE"}:
            normal_block_reason = f"Blocked: risk is {risk}"
        elif entry_quality in {"LATE", "DO_NOT_CHASE"}:
            normal_block_reason = f"Blocked: entry quality is {entry_quality}"
        elif alert.scenario_conflict:
            normal_block_reason = "Blocked: scenario conflict"
        elif alert.extension_label in {"VERY_EXTENDED", "DO_NOT_CHASE"}:
            normal_block_reason = "Blocked: price is extremely extended"

        if normal_block_reason and not stock_only_allowed and not special_watch_only:
            block(normal_block_reason)
            return
        if normal_block_reason and not special_watch_only:
            alert.stock_only_heads_up_allowed = True
            alert.stock_only_heads_up_reason = normal_block_reason
            alert.phase3_heads_up_type = "STOCK_ONLY_WARNING"
        if alert.stock_only_heads_up_allowed:
            if normalize_option_quality_label(alert.option_quality) in {"STALE", "INVALID"} or not alert.option_tradable:
                alert.option_stale_did_not_block_heads_up = True
                warning = "Option quote stale/missing — stock setup only."
                if warning not in alert.scenario_warnings:
                    alert.scenario_warnings.append(warning)
            if stage == "LATE" or entry_quality == "LATE":
                for warning in ("Late warning — do not chase.", "Wait for pullback/retest before entry."):
                    if warning not in alert.scenario_warnings:
                        alert.scenario_warnings.append(warning)
            if risk == "DO_NOT_CHASE" or stage == "DO_NOT_CHASE" or entry_quality == "DO_NOT_CHASE":
                warning = "Do Not Chase — watch only."
                if warning not in alert.scenario_warnings:
                    alert.scenario_warnings.append(warning)

        direction_conflict = self.phase2_direction_conflict(alert, scenario_direction)
        alert_direction = str(alert.direction or "").upper()
        if alert_direction in {"BULLISH", "BEARISH"} and alert_direction != scenario_direction:
            direction_conflict = True
        if direction_conflict:
            warning = "Legacy/Phase 2 conflict present — confirm manually."
            if warning not in alert.scenario_warnings:
                alert.scenario_warnings.insert(0, warning)
            strategy_warning = "Direction conflict: setup label does not match alert direction"
            if strategy_warning not in alert.strategy_warnings:
                alert.strategy_warnings.insert(0, strategy_warning)

        good_position = (
            stage in {"GOOD_POSITION", "CONFIRMED"}
            and int(alert.scenario_score or scenario.get("score") or 0)
            >= int(config.get("phase3_good_position_min_scenario_score", 85))
            and int(alert.stock_setup_score or alert.strategy_confidence_score or 0)
            >= int(config.get("phase3_good_position_min_stock_score", 70))
            and int(alert.confirmation_score or 0)
            >= int(config.get("phase3_good_position_min_confirmation_score", 60))
            and risk in {"LOW", "MEDIUM"}
            and entry_quality in {"GOOD_POSITION", "EARLY"}
            and alert.extension_label not in {"EXTENDED", "VERY_EXTENDED", "DO_NOT_CHASE"}
        )
        if not alert.stock_only_heads_up_allowed:
            alert.phase3_heads_up_type = "GOOD_POSITION" if good_position else "EARLY_WATCH"
        alert.phase3_heads_up_eligible = True
        if not config.get("phase3_heads_up_sms_enabled", True):
            block("Phase 3 heads-up SMS disabled")
            return

        key = self.phase3_heads_up_state_key(alert)
        alert.phase3_heads_up_dedupe_key = phase3_heads_up_dedupe_key(alert)
        alert.phase3_heads_up_message_fingerprint = phase3_heads_up_message_fingerprint(alert)
        self.state_store.load()
        dedupe_minutes = int(config.get("phase3_heads_up_dedupe_minutes", 15))
        current_record = phase3_heads_up_record(alert)
        previous_record = self.state_store.get_phase3_heads_up_record(alert.symbol)
        allowed, dedupe_reason, last = phase3_heads_up_dedupe_decision(
            previous_record,
            current_record,
            dedupe_minutes,
        )
        if previous_record is None:
            legacy_last = self.state_store.get_last_alert_time(key)
            if legacy_last and (now_utc() - legacy_last).total_seconds() <= dedupe_minutes * 60:
                allowed = False
                dedupe_reason = "duplicate Phase 3 heads-up blocked by legacy cooldown"
                last = legacy_last
        alert.phase3_heads_up_dedupe_reason = dedupe_reason
        if not allowed and last:
            next_eligible = last + timedelta(minutes=dedupe_minutes)
            alert.phase3_heads_up_dedupe_blocked = True
            alert.phase3_heads_up_last_sent_time = last.isoformat()
            alert.phase3_heads_up_next_eligible_time = next_eligible.isoformat()
            alert.phase3_heads_up_dedupe_minutes_remaining = round(
                max(0.0, (next_eligible - now_utc()).total_seconds() / 60.0),
                1,
            )
            block(dedupe_reason)
            return

        alert.phase3_heads_up_sent = True
        alert.phase3_heads_up_block_reason = ""
        alert.phase3_heads_up_final_decision = (
            "TELEGRAM_ATTEMPTED" if alert.stock_only_heads_up_allowed else "PHASE3_HEADS_UP"
        )
        alert.phase3_heads_up_final_block_reason = ""
        alert.text_alert_reason = "Phase 3 heads-up only: confirm on chart"
        alert.notes.append("Phase 3 heads-up only: confirm on chart")
        alert.phase3_heads_up_message_preview = phase3_heads_up_message(alert)
        claim_time = now_utc()
        self.state_store.set_last_alert_time(key, claim_time)
        self.state_store.set_phase3_heads_up_record(alert, claim_time)
        self.state_store.save()

    def grade_alert(self, alert: Alert, snap: SymbolSnapshot, market_context: Optional[Dict[str, str]]) -> Alert:
        quality_config = self.config.get("alert_quality", {})
        direction = self.infer_alert_direction(alert)
        alert.direction = direction
        alert.market_alignment = self.market_alignment_for(direction, market_context)

        score = 0
        reasons: List[str] = []
        data_quality = snapshot_data_quality(snap, self.config)
        age = bar_age_minutes(snap.latest_bar)
        max_age = float(quality_config.get("max_sms_bar_age_minutes", 2.0))
        if data_quality == "Fresh" and age is not None and age <= max_age:
            score += 15
        else:
            reasons.append(f"data not fresh enough ({data_quality})")

        fast_abs = abs(alert.fast_move_pct or 0.0)
        day_abs = abs(alert.day_move_pct or 0.0)
        rvol = alert.relative_volume or 0.0
        if fast_abs >= 1.0:
            score += 20
        elif fast_abs >= 0.5:
            score += 12
        elif fast_abs >= 0.25:
            score += 6
        if day_abs >= 3.0:
            score += 16
        elif day_abs >= 1.5:
            score += 10
        elif day_abs >= 0.75:
            score += 5
        if rvol >= 2.0:
            score += 25
        elif rvol >= float(quality_config.get("min_sms_rvol", 1.5)):
            score += 18
        elif rvol >= 1.0:
            score += 8
            reasons.append("RVOL is only moderate")
        else:
            score -= 35
            reasons.append("RVOL below text-alert threshold")

        hold_ok = self.breakout_hold_ok(alert, snap)
        immediate_ok = self.immediate_break_ok(alert, snap)
        break_confirmed = hold_ok or immediate_ok
        is_watch = self.is_watch_alert(alert)
        fast_opposes_setup = self.fast_move_opposes_setup(
            alert, float(quality_config.get("block_text_when_fast_move_opposes_setup_pct", 0.08))
        )
        if fast_opposes_setup:
            score -= 20
            reasons.append("latest fast move opposes setup direction")
        category_upper = alert.category.upper()
        generic_day_conflict = False
        if "BREAK" not in category_upper and not is_watch:
            min_conflict_day = float(quality_config.get("generic_day_conflict_min_day_pct", 1.0))
            max_conflict_fast = float(quality_config.get("generic_day_conflict_max_fast_pct", 0.25))
            day_move_value = alert.day_move_pct or 0.0
            generic_day_conflict = (
                (direction == "BULLISH" and day_move_value <= -min_conflict_day)
                or (direction == "BEARISH" and day_move_value >= min_conflict_day)
            ) and fast_abs <= max_conflict_fast
            if generic_day_conflict:
                score -= 35
                reasons.append("day trend conflicts with small fast move")
        break_distance = self.breakout_distance_pct(alert, snap)
        break_too_extended = False
        if "BREAK" in alert.category.upper() and break_distance is not None:
            max_break_distance = self.max_allowed_break_distance_pct(alert)
            break_too_extended = break_distance > max_break_distance
            if break_too_extended:
                reasons.append(f"break already extended {break_distance:.2f}% from level")
        if "BREAK" in alert.category.upper():
            score += 12
            if hold_ok:
                score += 10
            elif immediate_ok:
                score += 8
                reasons.append("fast clean break before hold confirmation")
            else:
                score -= 15
                reasons.append("breakout/breakdown has not held long enough")
        if alert.headline:
            reasons.append("fresh news present as context only")

        if option_quality_is_tradable(alert.option_quality):
            score += 15
            opt_score = alert.options_score or 0
            if opt_score >= 80:
                score += 10
            elif opt_score >= int(quality_config.get("min_sms_options_score", 65)):
                score += 6
            if alert.option_spread_pct is not None and alert.option_spread_pct <= 5.0:
                score += 8
            elif alert.option_spread_pct is not None and alert.option_spread_pct <= float(quality_config.get("max_sms_option_spread_pct", 8.0)):
                score += 4
        else:
            score -= 10
            reasons.append(f"option quality is {alert.option_quality or 'unknown'}")

        if alert.market_alignment == "ALIGNED":
            score += 15
        elif alert.market_alignment == "MIXED":
            score += 3
            reasons.append("SPY/QQQ market read is mixed")
        elif alert.market_alignment == "OPPOSED":
            score -= 25
            reasons.append("SPY/QQQ are opposing the setup")
        else:
            reasons.append("SPY/QQQ alignment unknown")

        strong_fast_break = (
            "BREAK" in alert.category.upper()
            and not is_watch
            and break_confirmed
            and self.fast_move_aligned(alert, float(quality_config.get("strong_fast_break_min_fast_move_pct", 0.75)))
            and rvol >= float(quality_config.get("strong_fast_break_min_rvol", 1.25))
            and not break_too_extended
            and option_quality_is_tradable(alert.option_quality)
            and (alert.options_score or 0) >= int(quality_config.get("min_sms_options_score", 65))
        )
        max_score = 100
        min_sms_rvol = float(quality_config.get("min_sms_rvol", 1.5))
        if rvol < 1.0:
            max_score = min(max_score, 39)
        elif rvol < min_sms_rvol and not strong_fast_break:
            max_score = min(max_score, 54)
        if alert.market_alignment == "OPPOSED":
            max_score = min(max_score, 54)
        if not break_confirmed:
            max_score = min(max_score, 54)
        if break_too_extended:
            max_score = min(max_score, 54)
        if fast_opposes_setup:
            max_score = min(max_score, 54)
        if generic_day_conflict:
            max_score = min(max_score, 54)
        if is_watch:
            max_score = min(max_score, 54)
        if not option_quality_is_tradable(alert.option_quality) or (alert.options_score or 0) < int(quality_config.get("min_sms_options_score", 65)):
            max_score = min(max_score, 54)
        strategy_warning_text = " | ".join(alert.strategy_warnings).lower()
        strategy_direction_conflict = self.phase2_direction_conflict(alert, direction)
        phase2_conflict = any(
            phrase in strategy_warning_text
            for phrase in (
                "contradicting strategies",
                "candle quality contradicts",
                "market regime is opposing",
                "opposing the setup",
            )
        )
        min_confirmation_score = int(quality_config.get("min_sms_confirmation_score", 55))
        weak_confirmation = (
            alert.confirmation_score is not None
            and alert.confirmation_score < min_confirmation_score
        )
        high_phase2_risk = alert.risk_label in {"HIGH", "DO_NOT_CHASE"}
        if strategy_direction_conflict:
            reasons.append("Phase 2 setup direction conflicts with alert direction")
        if phase2_conflict:
            reasons.append("Phase 2 has conflicting confirmation warnings")
        if weak_confirmation:
            reasons.append("Phase 2 confirmation below SMS threshold")
        if high_phase2_risk:
            reasons.append(f"Phase 2 risk is {alert.risk_label}")
        max_score = min(max_score, self.phase2_grade_cap(alert, direction))

        score = min(score, max_score)
        score = int(max(0, min(100, score)))
        alert.alert_score = score
        if score >= 85:
            alert.alert_grade = "A+"
        elif score >= 70:
            alert.alert_grade = "A"
        elif score >= 55:
            alert.alert_grade = "B"
        elif score >= 40:
            alert.alert_grade = "C"
        else:
            alert.alert_grade = "Avoid"
        if alert.alert_grade == "A+" and not self.phase2_allows_a_plus(alert, direction):
            alert.alert_grade = "A"

        grade_rank = {"Avoid": 0, "C": 1, "B": 2, "A": 3, "A+": 4}
        min_grade = quality_config.get("sms_min_grade", "B")
        sms_allowed = grade_rank.get(alert.alert_grade, 0) >= grade_rank.get(min_grade, 2)
        sms_allowed = sms_allowed and score >= int(quality_config.get("min_sms_score", 55))
        sms_allowed = sms_allowed and not is_watch
        sms_allowed = sms_allowed and (rvol >= float(quality_config.get("min_sms_rvol", 1.5)) or strong_fast_break)
        sms_allowed = sms_allowed and break_confirmed
        sms_allowed = sms_allowed and not break_too_extended
        sms_allowed = sms_allowed and not fast_opposes_setup
        sms_allowed = sms_allowed and option_quality_is_tradable(alert.option_quality)
        sms_allowed = sms_allowed and (alert.options_score or 0) >= int(quality_config.get("min_sms_options_score", 65))
        if alert.option_spread_pct is not None:
            sms_allowed = sms_allowed and alert.option_spread_pct <= float(quality_config.get("max_sms_option_spread_pct", 8.0))
        if alert.option_contract and "Indicative" in " | ".join(alert.notes):
            sms_allowed = sms_allowed and bool(quality_config.get("allow_indicative_sms", True))
        if quality_config.get("market_alignment_required", True):
            if strong_fast_break and quality_config.get("allow_unknown_market_for_strong_fast_break", True):
                sms_allowed = sms_allowed and alert.market_alignment in {"ALIGNED", "MIXED", "UNKNOWN"}
            else:
                sms_allowed = sms_allowed and alert.market_alignment in {"ALIGNED", "MIXED"}
        strategy_config = self.config.get("strategy_engine", {})
        min_strategy_score = int(strategy_config.get("min_strategy_score_to_alert", 60))
        if (
            strategy_config.get("enabled", True)
            and alert.primary_setup
            and (alert.strategy_confidence_score or 0) < min_strategy_score
        ):
            sms_allowed = False
            reasons.append("strategy confidence below alert threshold")
        if strategy_direction_conflict:
            sms_allowed = False
        if phase2_conflict:
            sms_allowed = False
        if weak_confirmation:
            sms_allowed = False
        if high_phase2_risk:
            sms_allowed = False
        phase2_sms_blocks = self.phase2_sms_block_reasons(alert, direction)
        if phase2_sms_blocks:
            sms_allowed = False
            for reason in phase2_sms_blocks:
                if reason not in reasons:
                    reasons.append(reason)
                warning = reason
                if reason.startswith("Direction conflict"):
                    warning = "Direction conflict: setup label does not match alert direction"
                if warning not in alert.strategy_warnings:
                    alert.strategy_warnings.insert(0, warning)
        scenario_config = self.config.get("scenario_engine", {})
        if scenario_config.get("control_sms", False) and alert.scenario_top:
            scenario_stage = (alert.scenario_stage or alert.scenario_top.get("stage") or "WATCHING").upper()
            scenario_score = int(alert.scenario_score or alert.scenario_top.get("score") or 0)
            stock_setup_score = int(alert.stock_setup_score or alert.strategy_confidence_score or 0)
            min_stock_setup = int(scenario_config.get("sms_min_stock_setup_score", 70))
            min_confirmation = int(scenario_config.get("sms_min_confirmation_score", 60))
            strong_stock = int(scenario_config.get("sms_strong_stock_setup_score", 85))
            strong_confirmation = int(scenario_config.get("sms_strong_confirmation_score", 70))
            if scenario_config.get("sms_require_good_stage", True) and scenario_stage not in {"CONFIRMED", "GOOD_POSITION"}:
                sms_allowed = False
                reasons.append(f"Scenario stage is {scenario_stage}")
            if scenario_config.get("sms_block_scenario_conflict", True) and alert.scenario_conflict:
                sms_allowed = False
                reasons.append("Scenario conflict")
            if stock_setup_score < min_stock_setup or (alert.confirmation_score or 0) < min_confirmation:
                sms_allowed = False
            if alert.option_feed_status in {"INDICATIVE", "UNAVAILABLE"} and scenario_config.get("opra_unavailable_require_stronger_sms", True):
                if stock_setup_score < strong_stock or (alert.confirmation_score or 0) < strong_confirmation:
                    sms_allowed = False
                    reasons.append("Option feed is indicative; stronger stock confirmation required")
            if scenario_score < int(scenario_config.get("min_dashboard_score", 55)):
                alert.notes.append("scenario score below dashboard threshold")
        if alert.risk_label == "DO_NOT_CHASE":
            sms_allowed = False
            reasons.append("strategy risk is DO_NOT_CHASE")
        if sms_allowed and self.orb_sms_blocked_by_dedupe(alert):
            sms_allowed = False
            reasons.append("repeated ORB SMS blocked until confirmation improves")
        if sms_allowed and self.is_late_day_generic_alert(alert) and not self.late_day_generic_has_fresh_push(alert):
            sms_allowed = False
            reasons.append("late-day repeat needs stronger fresh push")

        alert.sms_allowed = bool(sms_allowed)
        watch_allowed = False
        opposed_bearish_watch_allowed = False
        if is_watch:
            watch_rvol_min = float(quality_config.get("watch_text_min_rvol", quality_config.get("watch_min_rvol", 0.8)))
            watch_min_score = int(quality_config.get("watch_text_min_score", quality_config.get("watch_min_score", 35)))
            watch_min_options_score = int(
                quality_config.get("watch_text_min_options_score", quality_config.get("min_sms_options_score", 65))
            )
            watch_allowed = data_quality == "Fresh"
            watch_allowed = watch_allowed and age is not None and age <= max_age
            watch_allowed = watch_allowed and score >= watch_min_score
            watch_allowed = watch_allowed and rvol >= watch_rvol_min
            watch_allowed = watch_allowed and option_quality_is_tradable(alert.option_quality)
            watch_allowed = watch_allowed and (alert.options_score or 0) >= watch_min_options_score
            if alert.option_spread_pct is not None:
                watch_allowed = watch_allowed and alert.option_spread_pct <= float(
                    quality_config.get("max_sms_option_spread_pct", 8.0)
                )
            if quality_config.get("watch_market_alignment_required", quality_config.get("market_alignment_required", True)):
                watch_allowed = watch_allowed and alert.market_alignment in {"ALIGNED", "MIXED"}
            elif quality_config.get("watch_block_opposed_market", True):
                watch_allowed = watch_allowed and alert.market_alignment != "OPPOSED"
            watch_allowed = watch_allowed and not fast_opposes_setup
            if not watch_allowed:
                opposed_bearish_watch_allowed = self.market_opposed_bearish_watch_allowed(
                    alert,
                    data_quality,
                    age,
                    max_age,
                    score,
                    rvol,
                    fast_opposes_setup,
                    break_distance,
                )
                watch_allowed = opposed_bearish_watch_allowed
        if not watch_allowed and not alert.sms_allowed:
            opposed_bearish_watch_allowed = self.market_opposed_bearish_watch_allowed(
                alert,
                data_quality,
                age,
                max_age,
                score,
                rvol,
                fast_opposes_setup,
                break_distance,
            )
            watch_allowed = opposed_bearish_watch_allowed
        alert.watch_allowed = bool(watch_allowed)
        self.evaluate_phase3_heads_up(alert, snap, data_quality, market_context)
        if alert.sms_allowed:
            if immediate_ok and not hold_ok:
                alert.text_alert_reason = "passed fast clean-break, freshness, volume, market, and option-quality checks"
            else:
                alert.text_alert_reason = "passed freshness, volume, market, breakout-hold, and option-quality checks"
        elif alert.watch_allowed:
            if opposed_bearish_watch_allowed:
                alert.text_alert_reason = "watch only: bearish stock-specific move, but SPY/QQQ are not confirming yet"
            elif "FAST IMPULSE" in alert.category.upper():
                alert.text_alert_reason = "watch only: fast impulse, waiting for continuation or hold"
            else:
                alert.text_alert_reason = "watch only: near key level, waiting for confirmation"
        elif alert.phase3_heads_up_sent:
            alert.text_alert_reason = "Phase 3 heads-up only: confirm on chart"
        else:
            alert.text_alert_reason = "; ".join(reasons[:4]) or "below text-alert threshold"
        alert.notes.append(f"alert grade: {alert.alert_grade} ({alert.alert_score}/100)")
        alert.notes.append(f"market alignment: {alert.market_alignment}")
        if alert.watch_allowed:
            alert.notes.append("watch only: waiting for clean break/hold confirmation")
        if not alert.sms_allowed and not alert.phase3_heads_up_sent:
            alert.notes.append(f"text alert skipped: {alert.text_alert_reason}")
        structural_levels = dict(alert.strategy_levels or {})
        for name, value in {
            "pmh": snap.premarket_high,
            "pml": snap.premarket_low,
            "opening_range_high": snap.opening_range_high,
            "opening_range_low": snap.opening_range_low,
        }.items():
            if value is not None:
                structural_levels.setdefault(name, value)
        if snap.recent_bars:
            recent = snap.recent_bars[-10:]
            structural_levels.setdefault("recent_swing_low", min(bar.l for bar in recent))
            structural_levels.setdefault("recent_swing_high", max(bar.h for bar in recent))
        alert.strategy_levels = structural_levels
        apply_risk_invalidation(alert)
        assign_professional_alert_tier(alert)
        return alert

    def keep_best_text_alert_per_direction(self, alerts: List[Alert]) -> None:
        best_by_key: Dict[Tuple[str, str, str], Alert] = {}
        text_alerts = [alert for alert in alerts if alert.sms_allowed or alert.watch_allowed or alert.phase3_heads_up_sent]
        for alert in text_alerts:
            direction = alert.direction or self.infer_alert_direction(alert)
            level = "SMS" if alert.sms_allowed else "WATCH" if alert.watch_allowed else "PHASE3_HEADS_UP"
            key = (alert.symbol, direction, level)
            current = best_by_key.get(key)
            if current is None or self.alert_priority(alert) < self.alert_priority(current) or (
                self.alert_priority(alert) == self.alert_priority(current)
                and (alert.alert_score or 0) > (current.alert_score or 0)
            ):
                best_by_key[key] = alert
        for alert in text_alerts:
            direction = alert.direction or self.infer_alert_direction(alert)
            level = "SMS" if alert.sms_allowed else "WATCH" if alert.watch_allowed else "PHASE3_HEADS_UP"
            if best_by_key.get((alert.symbol, direction, level)) is alert:
                continue
            if alert.sms_allowed:
                alert.sms_allowed = False
                alert.text_alert_reason = "weaker duplicate text in same scan"
                alert.notes.append("text alert skipped: weaker duplicate text in same scan")
            elif alert.watch_allowed:
                alert.watch_allowed = False
                alert.text_alert_reason = "weaker duplicate watch in same scan"
                alert.notes.append("watch text skipped: weaker duplicate watch in same scan")
            elif alert.phase3_heads_up_sent:
                alert.phase3_heads_up_sent = False
                alert.phase3_heads_up_block_reason = "weaker duplicate Phase 3 heads-up in same scan"
                alert.phase3_heads_up_dedupe_blocked = True
                alert.notes.append("Phase 3 heads-up skipped: weaker duplicate in same scan")

    def latest_move_direction(self, alert: Alert) -> str:
        move = alert.fast_move_pct
        if move is None or abs(move) < 0.08:
            move = alert.day_move_pct
        if move is not None and move > 0:
            return "BULLISH"
        if move is not None and move < 0:
            return "BEARISH"
        return "NEUTRAL"

    def keep_watch_alerts_aligned_with_latest_move(self, alerts: List[Alert]) -> None:
        watch_by_symbol: Dict[str, List[Alert]] = {}
        for alert in alerts:
            if alert.watch_allowed:
                watch_by_symbol.setdefault(alert.symbol, []).append(alert)
        for symbol_alerts in watch_by_symbol.values():
            directions = {alert.direction or self.infer_alert_direction(alert) for alert in symbol_alerts}
            if not {"BULLISH", "BEARISH"}.issubset(directions):
                continue
            preferred = self.latest_move_direction(symbol_alerts[0])
            if preferred not in {"BULLISH", "BEARISH"}:
                continue
            for alert in symbol_alerts:
                direction = alert.direction or self.infer_alert_direction(alert)
                if direction == preferred:
                    alert.notes.append(f"watch text kept: latest move favors {preferred.lower()}")
                    continue
                alert.watch_allowed = False
                alert.text_alert_reason = f"opposite same-scan watch suppressed; latest move favors {preferred.lower()}"
                alert.notes.append(f"watch text skipped: latest move favors {preferred.lower()}")

    def maybe_reversal_watch_after_opposite_watch(
        self,
        snap: SymbolSnapshot,
        latest: Bar,
        fast_move: float,
        day_move: float,
        rel_vol: Optional[float],
        notes: List[str],
    ) -> Optional[Alert]:
        quality_config = self.config.get("alert_quality", {})
        if not quality_config.get("trend_flip_after_watch_enabled", True):
            return None
        min_fast = float(quality_config.get("trend_flip_after_watch_min_fast_move_pct", 0.12))
        direction = "BULLISH" if fast_move >= min_fast else "BEARISH" if fast_move <= -min_fast else "NEUTRAL"
        if direction == "NEUTRAL":
            return None
        lookback = int(quality_config.get("trend_flip_after_watch_lookback_seconds", 900))
        if not self.opposite_text_seen_recently(snap.symbol, direction, "WATCH", lookback):
            return None
        min_rvol = float(quality_config.get("trend_flip_after_watch_min_rvol", 1.0))
        if (rel_vol or 0.0) < min_rvol:
            return None
        selection = snap.best_call if direction == "BULLISH" else snap.best_put
        min_options_score = int(quality_config.get("watch_text_min_options_score", quality_config.get("min_sms_options_score", 65)))
        if not option_quality_is_tradable(selection.quality) or selection.score < min_options_score:
            return None
        return Alert(
            symbol=snap.symbol,
            timestamp=now_utc(),
            category="WATCH REVERSAL UP" if direction == "BULLISH" else "WATCH REVERSAL DOWN",
            price=latest.c,
            fast_move_pct=fast_move,
            day_move_pct=day_move,
            relative_volume=rel_vol,
            premarket_high=snap.premarket_high,
            premarket_low=snap.premarket_low,
            opening_range_high=snap.opening_range_high,
            opening_range_low=snap.opening_range_low,
            headline=snap.latest_news.headline if snap.latest_news else None,
            url=snap.latest_news.url if snap.latest_news else None,
            notes=notes + ["reversal after recent opposite watch"],
            setup_level="WATCH",
            trigger_level=latest.c,
        )

    def maybe_failed_breakout_watch(
        self,
        snap: SymbolSnapshot,
        latest: Bar,
        fast_move: float,
        day_move: float,
        rel_vol: Optional[float],
        notes: List[str],
    ) -> Optional[Alert]:
        quality_config = self.config.get("alert_quality", {})
        if not quality_config.get("failed_breakout_watch_enabled", True):
            return None

        min_fast = float(quality_config.get("failed_breakout_watch_min_fast_move_pct", 0.12))
        lookback = max(2, int(quality_config.get("failed_breakout_watch_lookback_bars", 8)))
        recent = snap.recent_bars[-lookback:]
        if len(recent) < 2:
            return None

        buf_pct = self.config.get("opening_range_break_buffer_pct", 0.03) / 100.0
        max_distance = float(quality_config.get("failed_breakout_watch_max_distance_pct", 0.60))
        level: Optional[float] = None
        direction = "NEUTRAL"
        category = ""

        if snap.opening_range_high is not None and fast_move <= -min_fast:
            broke_above = any(bar.h > snap.opening_range_high * (1 + buf_pct) for bar in recent[:-1])
            slipped_under = latest.c < snap.opening_range_high
            distance = pct_change(snap.opening_range_high, latest.c) if latest.c > 0 else max_distance + 1
            if broke_above and slipped_under and distance <= max_distance:
                level = snap.opening_range_high
                direction = "BEARISH"
                category = "WATCH FAILED BREAKOUT DOWN"

        if (
            direction == "NEUTRAL"
            and snap.opening_range_low is not None
            and fast_move >= min_fast
        ):
            broke_below = any(bar.l < snap.opening_range_low * (1 - buf_pct) for bar in recent[:-1])
            reclaimed = latest.c > snap.opening_range_low
            distance = pct_change(latest.c, snap.opening_range_low) if snap.opening_range_low > 0 else max_distance + 1
            if broke_below and reclaimed and distance <= max_distance:
                level = snap.opening_range_low
                direction = "BULLISH"
                category = "WATCH FAILED BREAKDOWN UP"

        if direction == "NEUTRAL" or level is None:
            return None

        return Alert(
            symbol=snap.symbol,
            timestamp=now_utc(),
            category=category,
            price=latest.c,
            fast_move_pct=fast_move,
            day_move_pct=day_move,
            relative_volume=rel_vol,
            premarket_high=snap.premarket_high,
            premarket_low=snap.premarket_low,
            opening_range_high=snap.opening_range_high,
            opening_range_low=snap.opening_range_low,
            headline=snap.latest_news.headline if snap.latest_news else None,
            url=snap.latest_news.url if snap.latest_news else None,
            notes=notes + ["failed opening-range breakout reversal"],
            setup_level="WATCH",
            trigger_level=level,
        )

    def maybe_sustained_trend_watch(
        self,
        snap: SymbolSnapshot,
        latest: Bar,
        fast_move: float,
        day_move: float,
        rel_vol: Optional[float],
        notes: List[str],
    ) -> Optional[Alert]:
        quality_config = self.config.get("alert_quality", {})
        if not quality_config.get("sustained_trend_watch_enabled", True):
            return None
        min_rvol = float(quality_config.get("sustained_trend_watch_min_rvol", 0.45))
        if (rel_vol or 0.0) < min_rvol:
            return None

        lookback = max(4, int(quality_config.get("sustained_trend_watch_lookback_bars", 12)))
        if len(snap.recent_bars) < lookback + 1:
            return None
        window = snap.recent_bars[-(lookback + 1):]
        anchor = window[0]
        trend_move = pct_change(latest.c, anchor.c)
        min_move = float(quality_config.get("sustained_trend_watch_min_move_pct", 0.12))
        if snap.symbol in {"SPY", "QQQ"}:
            min_move = float(quality_config.get("sustained_trend_watch_min_index_move_pct", min_move))

        up_bars = sum(1 for previous, current in zip(window, window[1:]) if current.c >= previous.c)
        down_bars = lookback - up_bars
        min_ratio = float(quality_config.get("sustained_trend_watch_min_green_ratio", 0.58))
        up_ratio = up_bars / lookback
        down_ratio = down_bars / lookback

        direction = "NEUTRAL"
        trigger_level: Optional[float] = None
        if trend_move >= min_move and up_ratio >= min_ratio:
            above_or_high = snap.opening_range_high is None or latest.c >= snap.opening_range_high
            above_pm_high = snap.premarket_high is None or latest.c >= snap.premarket_high
            if above_or_high or above_pm_high:
                direction = "BULLISH"
                trigger_level = snap.opening_range_high or snap.premarket_high
        elif trend_move <= -min_move and down_ratio >= min_ratio:
            below_or_low = snap.opening_range_low is None or latest.c <= snap.opening_range_low
            below_pm_low = snap.premarket_low is None or latest.c <= snap.premarket_low
            if below_or_low or below_pm_low:
                direction = "BEARISH"
                trigger_level = snap.opening_range_low or snap.premarket_low

        if direction == "NEUTRAL":
            return None
        return Alert(
            symbol=snap.symbol,
            timestamp=now_utc(),
            category="WATCH SUSTAINED TREND UP" if direction == "BULLISH" else "WATCH SUSTAINED TREND DOWN",
            price=latest.c,
            fast_move_pct=fast_move,
            day_move_pct=day_move,
            relative_volume=rel_vol,
            premarket_high=snap.premarket_high,
            premarket_low=snap.premarket_low,
            opening_range_high=snap.opening_range_high,
            opening_range_low=snap.opening_range_low,
            headline=snap.latest_news.headline if snap.latest_news else None,
            url=snap.latest_news.url if snap.latest_news else None,
            notes=notes + [f"sustained {lookback}m trend move {trend_move:+.2f}%"],
            setup_level="WATCH",
            trigger_level=trigger_level,
        )

    def alert_sort_key(self, alert: Alert) -> tuple[int, int, int]:
        priority = self.alert_priority(alert)
        direction = alert.direction or self.infer_alert_direction(alert)
        latest_direction = self.latest_move_direction(alert)
        if self.is_watch_alert(alert) and latest_direction in {"BULLISH", "BEARISH"}:
            direction_bias = 0 if direction == latest_direction else 1
        else:
            direction_bias = 0
        return (priority, direction_bias, -(alert.alert_score or 0))

    def preferred_option_selection(self, alert: Alert, snap: SymbolSnapshot) -> OptionSelection:
        direction = alert.direction or self.infer_alert_direction(alert)
        return snap.best_put if direction == "BEARISH" else snap.best_call

    def strategy_levels_for_snapshot(self, snap: SymbolSnapshot) -> Dict[str, Optional[float]]:
        structure_levels = snap.multi_timeframe_context.get("levels", {}) if snap.multi_timeframe_context else {}
        return {
            "pmh": snap.premarket_high,
            "pml": snap.premarket_low,
            "pdh": structure_levels.get("pdh"),
            "pdl": structure_levels.get("pdl"),
            "pdc": structure_levels.get("pdc"),
            "hod": structure_levels.get("hod"),
            "lod": structure_levels.get("lod"),
            "opening_range_high": snap.opening_range_high,
            "opening_range_low": snap.opening_range_low,
            "opening_range_15_high": snap.opening_range_15_high,
            "opening_range_15_low": snap.opening_range_15_low,
        }

    def apply_aapl_bearish_continuation_label(
        self,
        alert: Alert,
        snap: SymbolSnapshot,
        market_context: Optional[Dict[str, str]],
    ) -> None:
        if snap.symbol != "AAPL" or not snap.latest_bar or len(snap.recent_bars) < 10:
            return
        direction = alert.direction or self.infer_alert_direction(alert)
        if direction != "BEARISH":
            return
        latest = snap.latest_bar
        current_vwap = strategy_vwap(snap.recent_bars)
        current_ema9 = strategy_ema([bar.c for bar in snap.recent_bars], 9)
        if not current_vwap or not current_ema9:
            return
        below_vwap_ema = latest.c < current_vwap and latest.c < current_ema9
        key_lows = [
            level
            for level in (snap.opening_range_low, snap.opening_range_15_low, snap.premarket_low)
            if isinstance(level, (int, float)) and level > 0
        ]
        below_key_low = bool(key_lows and latest.c < min(key_lows))
        market_alignment = self.market_alignment_for("BEARISH", market_context)
        market_not_opposing = market_alignment in {"ALIGNED", "MIXED", "UNKNOWN"}
        underside_levels = [current_ema9, current_vwap, *key_lows]
        retested_underside = any(latest.h >= level * 0.998 and latest.c < level for level in underside_levels)
        candle_rejects = latest.c < latest.o and latest.c <= latest.l + (latest.h - latest.l) * 0.35
        volume_ok = (alert.volume_label in {"NORMAL", "STRONG", "CLIMAX"}) or (alert.relative_volume or 0.0) >= 0.8
        not_extended = alert.extension_label in {None, "UNKNOWN", "NORMAL"}
        if not all([below_vwap_ema, below_key_low, market_not_opposing, retested_underside, candle_rejects, volume_ok, not_extended]):
            return

        previous = alert.primary_setup
        alert.primary_setup = "Bearish Trend Continuation - Pullback Rejecting"
        if previous and previous != alert.primary_setup and previous not in alert.secondary_setups:
            alert.secondary_setups.insert(0, previous)
        alert.strategy_direction = "bearish"
        alert.strategy_confidence_score = max(alert.strategy_confidence_score or 0, 82)
        alert.strategy_confidence_label = "HIGH" if (alert.strategy_confidence_score or 0) >= 80 else "MEDIUM"
        alert.confirmation_score = max(alert.confirmation_score or 0, 65)
        alert.confirmation_label = "STRONG" if (alert.confirmation_score or 0) >= 70 else "NORMAL"
        if alert.risk_label == "HIGH":
            alert.risk_label = "MEDIUM"
        if alert.entry_quality_label not in {"GOOD_POSITION", "EARLY"}:
            alert.entry_quality_label = "GOOD_POSITION"
        for reason in (
            "AAPL below VWAP and EMA9",
            "AAPL below opening range/premarket low",
            "Pullback rejected underside of EMA9/VWAP or breakdown level",
            "Candle closed weak after retest",
        ):
            if reason not in alert.strategy_reasons:
                alert.strategy_reasons.append(reason)
        alert.notes.append("AAPL bearish continuation: pullback rejected")

    def attach_strategy_context(
        self,
        alert: Alert,
        snap: SymbolSnapshot,
        market_context: Optional[Dict[str, str]],
        market_bars: Optional[Dict[str, List[Bar]]] = None,
    ) -> Alert:
        strategy_config = self.config.get("strategy_engine", {})
        if not strategy_config.get("enabled", True) or not snap.latest_bar or not snap.recent_bars:
            return alert
        direction = alert.direction or self.infer_alert_direction(alert)
        market_alignment = self.market_alignment_for(direction, market_context)
        summary = evaluate_strategy_suite(
            snap.symbol,
            snap.recent_bars,
            snap.latest_bar,
            self.config,
            self.strategy_levels_for_snapshot(snap),
            alert.relative_volume,
            market_alignment,
            market_bars,
            option_context={
                "option_feed_status": alert.option_feed_status,
                "option_tradability_score": alert.option_tradability_score,
                "option_tradable": alert.option_tradable,
            },
        )
        alert.primary_setup = summary.get("primary_setup")
        alert.secondary_setups = list(summary.get("secondary_setups") or [])
        alert.strategy_direction = summary.get("direction")
        alert.strategy_confidence_score = summary.get("confidence_score")
        alert.strategy_confidence_label = summary.get("confidence_label")
        alert.risk_label = summary.get("risk_label")
        alert.confirmation_score = summary.get("confirmation_score")
        alert.confirmation_label = summary.get("confirmation_label")
        alert.entry_quality_label = summary.get("entry_quality_label")
        alert.volume_label = summary.get("volume_label")
        alert.rvol_detail = summary.get("rvol")
        alert.candle_label = summary.get("candle_label")
        alert.candle_score = summary.get("candle_score")
        alert.extension_label = summary.get("extension_label")
        alert.extension_score = summary.get("extension_score")
        alert.relative_strength_label = summary.get("relative_strength_label")
        alert.relative_strength_score = summary.get("relative_strength_score")
        alert.market_regime = summary.get("market_regime")
        alert.regime_score = summary.get("regime_score", summary.get("market_score"))
        alert.market_score = summary.get("market_score")
        alert.regime_reason = summary.get("regime_reason")
        alert.spy_alignment = summary.get("spy_alignment")
        alert.qqq_alignment = summary.get("qqq_alignment")
        alert.aapl_relative_strength = summary.get("aapl_relative_strength")
        alert.volume_state = summary.get("volume_state")
        alert.volatility_state = summary.get("volatility_state")
        alert.pressure_label = summary.get("pressure_label")
        alert.pressure_score = summary.get("pressure_score")
        alert.scenario_top = summary.get("scenario_top")
        alert.scenario_second = summary.get("scenario_second")
        alert.scenario_score = summary.get("scenario_score")
        alert.scenario_stage = summary.get("scenario_stage")
        alert.scenario_direction = summary.get("scenario_direction")
        alert.scenario_confidence_label = summary.get("scenario_confidence_label")
        alert.scenario_entry_quality_label = summary.get("scenario_entry_quality_label")
        alert.scenario_risk_label = summary.get("scenario_risk_label")
        alert.scenario_reasons = list(summary.get("scenario_reasons") or [])
        alert.scenario_warnings = list(summary.get("scenario_warnings") or [])
        alert.scenario_levels = dict(summary.get("scenario_levels") or {})
        alert.bullish_score = summary.get("bullish_score")
        alert.bearish_score = summary.get("bearish_score")
        alert.chop_score = summary.get("chop_score")
        alert.fakeout_score = summary.get("fakeout_score")
        alert.scenario_conflict = summary.get("scenario_conflict")
        alert.all_scenarios = list(summary.get("all_scenarios") or [])
        alert.stock_setup_score = summary.get("stock_setup_score")
        alert.stock_setup_valid = summary.get("stock_setup_valid")
        alert.option_tradability_score = summary.get("option_tradability_score")
        alert.option_feed_status = summary.get("option_feed_status")
        alert.option_tradable = summary.get("option_tradable")
        alert.scenario_alert_eligible = summary.get("scenario_alert_eligible")
        alert.scenario_would_sms = summary.get("scenario_would_sms")
        alert.scenario_alert_tier = summary.get("scenario_alert_tier")
        alert.scenario_alert_block_reason = summary.get("scenario_alert_block_reason")
        alert.sms_allowed_by_stock = summary.get("sms_allowed_by_stock")
        alert.sms_allowed_by_options = summary.get("sms_allowed_by_options")
        alert.sms_block_reason = summary.get("sms_block_reason")
        alert.scenario_sms_block_reason = summary.get("scenario_sms_block_reason")
        alert.scenario_sms_allowed = summary.get("scenario_sms_allowed")
        alert.stock_setup_score_reason = summary.get("stock_setup_score_reason")
        alert.professional_setup = dict(summary.get("professional_setup") or {})
        alert.setup_name = summary.get("setup_name")
        alert.setup_code = summary.get("setup_code")
        alert.setup_direction = summary.get("setup_direction")
        alert.setup_stage = summary.get("setup_stage")
        alert.setup_score = summary.get("setup_score")
        alert.setup_confidence = summary.get("setup_confidence")
        alert.setup_reason = summary.get("setup_reason")
        alert.setup_invalidation_level = summary.get("setup_invalidation_level")
        alert.setup_entry_quality = summary.get("setup_entry_quality")
        alert.setup_risk_label = summary.get("setup_risk_label")
        alert.setup_watch_text = summary.get("setup_watch_text")
        alert.setup_block_reason = summary.get("setup_block_reason")
        alert.strategy_reasons = list(summary.get("reasons") or [])
        alert.strategy_warnings = list(summary.get("warnings") or [])
        alert.strategy_levels = dict(summary.get("levels") or {})
        alert.strategy_results = list(summary.get("strategy_results") or [])
        structure = snap.multi_timeframe_context or {}
        alert.trend_1m = structure.get("trend_1m")
        alert.trend_5m = structure.get("trend_5m")
        alert.trend_15m = structure.get("trend_15m")
        alert.daily_trend = structure.get("daily_trend")
        alert.current_structure_bias = structure.get("current_bias")
        alert.structure_key_warning = structure.get("key_warning")
        alert.nearest_level_name = structure.get("nearest_level_name")
        alert.nearest_level_price = structure.get("nearest_level_price")
        alert.distance_to_key_level_pct = structure.get("distance_to_key_level_pct")
        alert.nearest_support = structure.get("nearest_support")
        alert.nearest_resistance = structure.get("nearest_resistance")
        alert.demand_zones = list(structure.get("demand_zones") or [])
        alert.supply_zones = list(structure.get("supply_zones") or [])
        alert.liquidity_above_highs = list(structure.get("liquidity_above_highs") or [])
        alert.liquidity_below_lows = list(structure.get("liquidity_below_lows") or [])
        alert.multi_timeframe_levels = dict(structure.get("levels") or {})
        if snap.latest_news:
            alert.latest_headline = snap.latest_news.headline
            alert.news_source = snap.latest_news.source
            alert.news_age_minutes = round(
                max(0.0, (alert.timestamp - snap.latest_news.published_at).total_seconds() / 60.0),
                2,
            )
            alert.news_sentiment_guess = news_sentiment_guess(snap.latest_news.headline)
        if (
            alert.strategy_direction in {"bullish", "bearish"}
            and alert.current_structure_bias in {"BULLISH", "BEARISH"}
            and alert.strategy_direction.upper() != alert.current_structure_bias
        ):
            warning = "Setup direction disagrees with higher timeframe structure"
            if warning not in alert.strategy_warnings:
                alert.strategy_warnings.append(warning)
        self.apply_mixed_signal_and_news_context(alert)
        self.apply_aapl_bearish_continuation_label(alert, snap, market_context)
        if alert.primary_setup:
            alert.notes.append(
                f"primary setup: {alert.primary_setup} ({alert.strategy_confidence_score} {alert.strategy_confidence_label})"
            )
        if alert.scenario_top:
            top_name = alert.scenario_top.get("scenario_name", "")
            top_stage = alert.scenario_top.get("stage", "")
            if top_name or top_stage:
                alert.notes.append(f"scenario: {top_name} {top_stage}".strip())
        if alert.risk_label:
            alert.notes.append(f"strategy risk: {alert.risk_label}")
        if alert.volume_label:
            alert.notes.append(f"volume quality: {alert.volume_label}")
        if alert.candle_label:
            alert.notes.append(f"candle quality: {alert.candle_label}")
        if alert.entry_quality_label and alert.entry_quality_label != "UNKNOWN":
            alert.notes.append(f"entry quality: {alert.entry_quality_label}")
        if alert.extension_label and alert.extension_label not in {"NORMAL", "UNKNOWN"}:
            alert.notes.append(f"extension: {alert.extension_label}")
        if alert.relative_strength_label and alert.relative_strength_label not in {"NEUTRAL", "UNKNOWN"}:
            alert.notes.append(f"relative strength: {alert.relative_strength_label}")
        if alert.market_regime and alert.market_regime != "UNKNOWN":
            alert.notes.append(f"market regime: {alert.market_regime}")
        if alert.current_structure_bias:
            alert.notes.append(
                f"structure: 1m {alert.trend_1m} | 5m {alert.trend_5m} | 15m {alert.trend_15m} | bias {alert.current_structure_bias}"
            )
        if alert.setup_name:
            alert.notes.append(
                f"official setup: {alert.setup_name} {alert.setup_stage or ''} ({alert.setup_score or 0} {alert.setup_confidence or ''})"
            )
        if alert.setup_name == "Mixed Signal" and alert.setup_reason:
            warning = f"MIXED_SIGNAL: {alert.setup_reason}"
            if warning not in alert.strategy_warnings:
                alert.strategy_warnings.insert(0, warning)
        if alert.regime_reason:
            alert.notes.append(f"regime reason: {alert.regime_reason}")
        if alert.pressure_label and alert.pressure_label != "UNKNOWN":
            alert.notes.append(f"pressure: {alert.pressure_label}")
        if alert.strategy_warnings:
            alert.notes.append(f"strategy warning: {alert.strategy_warnings[0]}")
        return alert

    def apply_mixed_signal_and_news_context(self, alert: Alert) -> None:
        alert.primary_setup_direction = str(alert.strategy_direction or "").upper() or None
        alert.phase3_scenario_direction = str(alert.scenario_direction or "").upper() or None
        alert.mixed_signal_detected = bool(
            alert.scenario_conflict
            or (
                alert.primary_setup_direction in {"BULLISH", "BEARISH"}
                and alert.phase3_scenario_direction in {"BULLISH", "BEARISH"}
                and alert.primary_setup_direction != alert.phase3_scenario_direction
            )
        )
        alert.conflict_warning_added = False
        if alert.mixed_signal_detected:
            primary_lower = str(alert.primary_setup or "").lower()
            scenario_lower = str((alert.scenario_top or {}).get("scenario_name") or "").lower()
            if "liquidity sweep" in primary_lower and "fail" in scenario_lower:
                alert.mixed_signal_reason = "Bullish sweep happened, but reclaim failed — bearish warning."
            elif "liquidity sweep" in primary_lower and alert.phase3_scenario_direction == "BULLISH":
                alert.mixed_signal_reason = "Bearish sweep happened, but price reclaimed support — bullish warning."
            else:
                alert.mixed_signal_reason = "Price swept/reclaimed one level, but failed to hold VWAP/EMA."
            warning = f"Mixed signal / conflict: {alert.mixed_signal_reason}"
            if warning not in alert.strategy_warnings:
                alert.strategy_warnings.insert(0, warning)
                alert.conflict_warning_added = True
        alert.news_context_present = bool(alert.headline)
        alert.latest_headline = alert.latest_headline or alert.headline
        alert.news_sentiment_guess = alert.news_sentiment_guess or (
            news_sentiment_guess(alert.latest_headline) if alert.latest_headline else None
        )
        alert.news_used_for_context_only = bool(alert.headline)
        alert.news_upgraded_alert = False
        if alert.headline:
            for warning in (
                "Fresh AAPL news present — context only. Confirm price reaction.",
            ):
                if warning not in alert.strategy_warnings:
                    alert.strategy_warnings.append(warning)

    def attach_option_context(self, alert: Alert, snap: SymbolSnapshot) -> Alert:
        selection = self.preferred_option_selection(alert, snap)
        contract = selection.contract
        alert.option_quality = normalize_option_quality_label(selection.quality)
        alert.option_quality_message = selection.details.get(
            "message", option_quality_message(alert.option_quality, bool(selection.details.get("is_0dte")))
        )
        alert.option_quality_reasons = list(selection.reasons)
        alert.option_days_to_expiration = selection.details.get("days_to_expiration")
        alert.option_is_0dte = selection.details.get("is_0dte")
        alert.option_strike_distance_pct = selection.details.get("strike_distance_pct")
        alert.option_liquidity_state = selection.details.get("liquidity_state")
        alert.option_time_state = selection.details.get("time_state")
        alert.option_stock_only_allowed = True
        alert.options_score = selection.score
        alert.option_tradable = selection.is_tradable()
        if contract:
            freshness = option_freshness_details(contract, self.config, alert.timestamp)
            if contract.is_simulated:
                alert.option_feed_status = "SIMULATED"
            elif contract.feed == "opra":
                alert.option_feed_status = "OPRA"
            elif contract.feed == "indicative":
                alert.option_feed_status = "INDICATIVE"
            else:
                alert.option_feed_status = "UNAVAILABLE"
        else:
            alert.option_feed_status = "UNAVAILABLE"
        alert.option_tradability_score = selection.score
        if contract:
            alert.option_contract = contract.symbol
            alert.option_type = "PUT" if contract.option_type == "P" else "CALL"
            alert.option_expiration = contract.expiration_date.isoformat()
            alert.option_strike = contract.strike
            alert.option_bid = contract.bid
            alert.option_ask = contract.ask
            alert.option_mid = contract.mid
            alert.option_spread_pct = contract.spread_pct
            alert.option_delta = contract.delta
            alert.option_iv = contract.implied_volatility
            alert.option_volume = contract.volume
            alert.option_open_interest = contract.open_interest
            alert.option_quote_timestamp_raw = freshness["quote_timestamp_raw"]
            alert.option_quote_timestamp_utc = freshness["quote_timestamp_utc"]
            alert.option_quote_age_seconds = freshness["quote_age_seconds"]
            alert.option_max_quote_age_seconds = freshness["max_allowed_quote_age_seconds"]
            alert.option_stale_reason = freshness["stale_reason"]
            alert.option_data_source = contract.feed or "unknown"
            alert.option_fallback_used = contract.feed == "indicative"
            alert.option_timestamp_source_field = freshness["timestamp_source_field"]
            alert.option_timestamp_extraction_failed = freshness["timestamp_extraction_failed"]
            alert.option_timestamp_available_fields = list(freshness["timestamp_available_fields"])
            alert.option_fallback_used = bool(alert.option_fallback_used or freshness["fallback_used"])
            alert.option_timestamp_fallback_type = freshness["fallback_type"]
            alert.option_fallback_timestamp_utc = freshness["fallback_timestamp_utc"]
            alert.notes.append(
                f"suggested option: {contract.symbol} ({alert.option_quality}, score {selection.score})"
            )
            if contract.is_simulated:
                alert.notes.append("option data is simulated dry-run data")
            elif contract.feed == "indicative":
                alert.notes.append("option feed: Indicative, not official OPRA quotes")
            elif contract.feed == "opra":
                alert.notes.append("option feed: OPRA")
        else:
            alert.notes.append(f"option warning: {alert.option_quality_message}")

        if selection.is_tradable():
            alert.notes.append("high confidence requires stock momentum + tradable option")
        else:
            reason = "; ".join(selection.reasons) if selection.reasons else alert.option_quality
            alert.notes.append(f"{alert.option_quality_message} ({reason})")
        return alert

    def evaluate_symbol(
        self,
        snap: SymbolSnapshot,
        market_context: Optional[Dict[str, str]] = None,
        market_bars: Optional[Dict[str, List[Bar]]] = None,
    ) -> List[Alert]:
        alerts: List[Alert] = []
        quality = snapshot_data_quality(snap, self.config)
        if quality in {"Stale", "Incomplete"}:
            return alerts
        if not self.passes_basic_filters(snap):
            return alerts

        latest = snap.latest_bar
        assert latest is not None
        bars = snap.recent_bars
        if not has_min_recent_bars(bars, self.config):
            return alerts

        anchor = bars[-(self.config["lookback_minutes_fast_move"] + 1)]
        fast_move = pct_change(latest.c, anchor.c)
        anchor_bar = session_anchor_bar(bars, self.config)
        if not anchor_bar:
            return alerts
        day_move = pct_change(latest.c, anchor_bar.o)
        rel_vol = self.compute_relative_volume(bars)
        notes: List[str] = []
        notes.append(f"data quality: {quality}")
        notes.append("RVOL is recent 1-minute relative volume")

        if snap.latest_news:
            notes.append("news catalyst detected")

        # 1) Fast move with volume
        if self.config["alert_rules"].get("fast_move", True):
            if abs(fast_move) >= self.config["fast_move_pct_threshold"] and (rel_vol or 0) >= self.config["relative_volume_threshold"]:
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="FAST MOVE",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline if snap.latest_news else None,
                        url=snap.latest_news.url if snap.latest_news else None,
                        notes=notes + [f"{self.config['lookback_minutes_fast_move']}m momentum spike"],
                    )
                )

        # 2) Standalone relative-volume surge
        if self.config["alert_rules"].get("high_relative_volume", True):
            if (rel_vol or 0) >= self.config["relative_volume_threshold"]:
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="HIGH RELATIVE VOLUME",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline if snap.latest_news else None,
                        url=snap.latest_news.url if snap.latest_news else None,
                        notes=notes + ["unusual volume vs recent 1-minute bars"],
                    )
                )

        # 3) Premarket high break
        if self.config["alert_rules"].get("premarket_high_break", True) and snap.premarket_high is not None:
            pm_watch_pct = self.config.get("premarket_watch_proximity_pct", 0.15) / 100.0
            pm_high_watch_start = snap.premarket_high * (1 - pm_watch_pct)
            if pm_high_watch_start <= latest.c <= snap.premarket_high:
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="WATCH PREMARKET HIGH BREAK",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline if snap.latest_news else None,
                        url=snap.latest_news.url if snap.latest_news else None,
                        notes=notes + ["near premarket high; wait for break/hold"],
                        setup_level="WATCH",
                        trigger_level=snap.premarket_high,
                    )
                )
            if latest.c > snap.premarket_high:
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="PREMARKET HIGH BREAK",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline if snap.latest_news else None,
                        url=snap.latest_news.url if snap.latest_news else None,
                        notes=notes + ["watch for continuation or rejection"],
                        setup_level="ALERT",
                        trigger_level=snap.premarket_high,
                    )
                )

        # 4) Premarket low break / flush
        if self.config["alert_rules"].get("premarket_low_break", True) and snap.premarket_low is not None:
            pm_watch_pct = self.config.get("premarket_watch_proximity_pct", 0.15) / 100.0
            pm_low_watch_start = snap.premarket_low * (1 + pm_watch_pct)
            if snap.premarket_low <= latest.c <= pm_low_watch_start:
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="WATCH PREMARKET LOW BREAK",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline if snap.latest_news else None,
                        url=snap.latest_news.url if snap.latest_news else None,
                        notes=notes + ["near premarket low; wait for break/hold"],
                        setup_level="WATCH",
                        trigger_level=snap.premarket_low,
                    )
                )
            if latest.c < snap.premarket_low:
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="PREMARKET LOW BREAK",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline if snap.latest_news else None,
                        url=snap.latest_news.url if snap.latest_news else None,
                        notes=notes + ["potential flush / trend down"],
                        setup_level="ALERT",
                        trigger_level=snap.premarket_low,
                    )
                )

        # 5) Opening range break
        if self.config["alert_rules"].get("opening_range_break", True) and opening_range_complete(bars, self.config):
            buf_pct = self.config["opening_range_break_buffer_pct"] / 100.0
            watch_pct = self.config.get("opening_range_watch_proximity_pct", 0.12) / 100.0
            if snap.opening_range_high is not None:
                high_watch_start = snap.opening_range_high * (1 - watch_pct)
                high_break_level = snap.opening_range_high * (1 + buf_pct)
                if high_watch_start <= latest.c <= high_break_level:
                    alerts.append(
                        Alert(
                            symbol=snap.symbol,
                            timestamp=now_utc(),
                            category="WATCH OPENING RANGE BREAK UP",
                            price=latest.c,
                            fast_move_pct=fast_move,
                            day_move_pct=day_move,
                            relative_volume=rel_vol,
                            premarket_high=snap.premarket_high,
                            premarket_low=snap.premarket_low,
                            opening_range_high=snap.opening_range_high,
                            opening_range_low=snap.opening_range_low,
                            headline=snap.latest_news.headline if snap.latest_news else None,
                            url=snap.latest_news.url if snap.latest_news else None,
                            notes=notes + ["near opening range high; wait for break/hold"],
                            setup_level="WATCH",
                            trigger_level=snap.opening_range_high,
                        )
                    )
            if snap.opening_range_high is not None and latest.c > snap.opening_range_high * (1 + buf_pct):
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="OPENING RANGE BREAK UP",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline if snap.latest_news else None,
                        url=snap.latest_news.url if snap.latest_news else None,
                        notes=notes + ["opening range breakout"],
                        setup_level="ALERT",
                        trigger_level=snap.opening_range_high,
                    )
                )
            if snap.opening_range_low is not None:
                low_watch_start = snap.opening_range_low * (1 + watch_pct)
                low_break_level = snap.opening_range_low * (1 - buf_pct)
                if low_break_level <= latest.c <= low_watch_start:
                    alerts.append(
                        Alert(
                            symbol=snap.symbol,
                            timestamp=now_utc(),
                            category="WATCH OPENING RANGE BREAK DOWN",
                            price=latest.c,
                            fast_move_pct=fast_move,
                            day_move_pct=day_move,
                            relative_volume=rel_vol,
                            premarket_high=snap.premarket_high,
                            premarket_low=snap.premarket_low,
                            opening_range_high=snap.opening_range_high,
                            opening_range_low=snap.opening_range_low,
                            headline=snap.latest_news.headline if snap.latest_news else None,
                            url=snap.latest_news.url if snap.latest_news else None,
                            notes=notes + ["near opening range low; wait for break/hold"],
                            setup_level="WATCH",
                            trigger_level=snap.opening_range_low,
                        )
                    )
            if snap.opening_range_low is not None and latest.c < snap.opening_range_low * (1 - buf_pct):
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="OPENING RANGE BREAK DOWN",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline if snap.latest_news else None,
                        url=snap.latest_news.url if snap.latest_news else None,
                        notes=notes + ["opening range breakdown"],
                        setup_level="ALERT",
                        trigger_level=snap.opening_range_low,
                    )
                )

        # 6) Big day mover with volume + news
        if (
            self.config["alert_rules"].get("news_catalyst", True)
            and not bool(self.config.get("news_context", {}).get("context_only", True))
        ):
            if abs(day_move) >= self.config["day_move_pct_threshold"] and (rel_vol or 0) >= self.config["relative_volume_threshold"] and snap.latest_news:
                alerts.append(
                    Alert(
                        symbol=snap.symbol,
                        timestamp=now_utc(),
                        category="CATALYST RUNNER",
                        price=latest.c,
                        fast_move_pct=fast_move,
                        day_move_pct=day_move,
                        relative_volume=rel_vol,
                        premarket_high=snap.premarket_high,
                        premarket_low=snap.premarket_low,
                        opening_range_high=snap.opening_range_high,
                        opening_range_low=snap.opening_range_low,
                        headline=snap.latest_news.headline,
                        url=snap.latest_news.url,
                        notes=notes + ["large move with news + volume"],
                    )
                )

        fast_impulse_watch = self.maybe_fast_impulse_watch(snap, latest, fast_move, day_move, rel_vol, notes)
        if fast_impulse_watch:
            alerts.append(fast_impulse_watch)
        reversal_watch = self.maybe_reversal_watch_after_opposite_watch(snap, latest, fast_move, day_move, rel_vol, notes)
        if reversal_watch:
            alerts.append(reversal_watch)
        failed_breakout_watch = self.maybe_failed_breakout_watch(snap, latest, fast_move, day_move, rel_vol, notes)
        if failed_breakout_watch:
            alerts.append(failed_breakout_watch)
        sustained_trend_watch = self.maybe_sustained_trend_watch(snap, latest, fast_move, day_move, rel_vol, notes)
        if sustained_trend_watch:
            alerts.append(sustained_trend_watch)

        enriched: List[Alert] = []
        for alert in alerts:
            with_options = self.attach_option_context(alert, snap)
            with_strategy = self.attach_strategy_context(with_options, snap, market_context, market_bars)
            enriched.append(self.grade_alert(with_strategy, snap, market_context))
        self.keep_watch_alerts_aligned_with_latest_move(enriched)
        enriched.sort(key=self.alert_sort_key)
        self.keep_best_text_alert_per_direction(enriched)
        return enriched

    def alert_priority(self, alert: Alert) -> int:
        category = alert.category.upper()
        is_watch = self.is_watch_alert(alert)
        if "BREAK" in category and not is_watch:
            return 0
        if is_watch:
            return 1
        if "FAST MOVE" in category or "HIGH RELATIVE VOLUME" in category:
            return 2
        return 3

    def cooldown_allows(self, alert: Alert) -> bool:
        last = self.state_store.get_last_alert_time(alert.dedupe_key())
        if not last:
            return True
        elapsed = (now_utc() - last).total_seconds()
        return elapsed >= self.config["alert_cooldown_seconds"]

    def text_cooldown_key(self, alert: Alert, prefix: str) -> str:
        direction = alert.direction or self.infer_alert_direction(alert) or "MOMENTUM"
        day_key = alert.timestamp.astimezone(ET).strftime("%Y-%m-%d")
        return f"{day_key}:{prefix}:{alert.symbol}:{direction}"

    def opposite_text_seen_recently(self, symbol: str, direction: str, prefix: str, lookback_seconds: int) -> bool:
        opposite = "BEARISH" if direction == "BULLISH" else "BULLISH"
        day_key = now_et().strftime("%Y-%m-%d")
        key = f"{day_key}:{prefix}:{symbol}:{opposite}"
        last = self.state_store.get_last_alert_time(key)
        if not last:
            return False
        return (now_utc() - last).total_seconds() <= lookback_seconds

    def opposite_sms_seen_recently(self, alert: Alert, lookback_seconds: int) -> bool:
        direction = alert.direction or self.infer_alert_direction(alert)
        return self.opposite_text_seen_recently(alert.symbol, direction, "SMS", lookback_seconds)

    def maybe_allow_trend_flip_watch(self, alert: Alert) -> None:
        if alert.sms_allowed or alert.watch_allowed:
            return
        quality_config = self.config.get("alert_quality", {})
        if not quality_config.get("trend_flip_watch_enabled", True):
            return
        lookback = int(quality_config.get("trend_flip_lookback_seconds", 3600))
        if not self.opposite_sms_seen_recently(alert, lookback):
            return
        min_rvol = float(quality_config.get("trend_flip_min_rvol", 1.0))
        min_options_score = int(quality_config.get("trend_flip_min_options_score", 60))
        if (alert.relative_volume or 0.0) < min_rvol:
            return
        if not option_quality_is_tradable(alert.option_quality) or (alert.options_score or 0) < min_options_score:
            return
        if alert.option_spread_pct is not None and alert.option_spread_pct > float(
            quality_config.get("max_sms_option_spread_pct", 8.0)
        ):
            return
        if "BREAK" not in alert.category.upper() and not self.fast_move_aligned(alert, 0.08):
            return
        alert.watch_allowed = True
        alert.setup_level = "WATCH"
        alert.text_alert_reason = "possible trend flip after prior opposite alert"
        alert.notes.append("watch only: possible trend flip after prior opposite alert")

    def text_cooldown_allows(self, key: str, cooldown_seconds: int) -> bool:
        last = self.state_store.get_last_alert_time(key)
        if not last:
            return True
        elapsed = (now_utc() - last).total_seconds()
        return elapsed >= cooldown_seconds

    def process_alert(self, alert: Alert) -> bool:
        quality_config = self.config.get("alert_quality", {})
        text_key: Optional[str] = None
        category_cooldown_allowed = self.cooldown_allows(alert)
        self.maybe_allow_trend_flip_watch(alert)
        self.apply_market_structure_decision_quality(alert)
        if not category_cooldown_allowed and not alert.sms_allowed and not alert.phase3_heads_up_sent:
            return False
        if alert.sms_allowed:
            cooldown_seconds = int(quality_config.get("sms_symbol_cooldown_seconds", 180))
            text_key = self.text_cooldown_key(alert, "SMS")
            if not self.text_cooldown_allows(text_key, cooldown_seconds):
                alert.sms_allowed = False
                alert.text_alert_reason = "symbol/direction text cooldown"
                alert.notes.append("text alert skipped: symbol/direction text cooldown")
                text_key = None
            elif not category_cooldown_allowed:
                alert.notes.append("text alert upgrade: prior same setup did not send SMS")
        elif alert.watch_allowed:
            cooldown_seconds = int(quality_config.get("watch_symbol_cooldown_seconds", 180))
            text_key = self.text_cooldown_key(alert, "WATCH")
            if not self.text_cooldown_allows(text_key, cooldown_seconds):
                alert.watch_allowed = False
                alert.text_alert_reason = "watch text cooldown"
                alert.notes.append("watch text skipped: symbol/direction cooldown")
                text_key = None
        elif alert.phase3_heads_up_sent:
            text_key = self.phase3_heads_up_state_key(alert)
        if not category_cooldown_allowed and not alert.sms_allowed and not alert.phase3_heads_up_sent:
            return False
        apply_risk_invalidation(alert)
        assign_professional_alert_tier(alert)
        logger.info(alert.short_summary())
        self.writer.write(alert)
        if self.post_alert_performance_enabled:
            self.post_alert_tracker.register(alert)
        self.notifier.send(alert)
        self.state_store.set_last_alert_time(alert.dedupe_key(), now_utc())
        if text_key:
            self.state_store.set_last_alert_time(text_key, now_utc())
            if alert.sms_allowed:
                self.record_orb_sms_state(alert)
        self.state_store.save()
        return True

    def run_once(self) -> int:
        if not in_extended_or_regular_session(self.config):
            logger.info("Outside scan window. No scan performed.")
            return 0
        snapshots = self.build_snapshots()
        if self.post_alert_performance_enabled:
            self.post_alert_tracker.update(snapshots, now=now_utc())
        market_context = self.build_market_context(snapshots)
        market_bars = {
            symbol: snap.recent_bars
            for symbol, snap in snapshots.items()
            if symbol in {"SPY", "QQQ"} and snap.recent_bars
        }
        count = 0
        for symbol in self.symbols:
            snap = snapshots.get(symbol)
            if not snap:
                continue
            if symbol == "AAPL":
                self.process_liquidity_sweep_telegram(snap)
            for alert in self.evaluate_symbol(snap, market_context, market_bars):
                if self.process_alert(alert):
                    count += 1
        return count

    def run_forever(self) -> None:
        logger.info("Scanner started for %s", ", ".join(self.symbols))
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("Stopped by user.")
                raise
            except Exception as exc:
                logger.exception("Loop error: %s", exc)
            time.sleep(int(self.config["scan_interval_seconds"]))


# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
def load_config(path: Optional[Path]) -> Dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and path.exists():
        user = json.loads(path.read_text())
        deep_update(config, user)
    apply_strategy_env_config(config)
    return config


def deep_update(base: Dict[str, Any], new: Dict[str, Any]) -> None:
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def apply_strategy_env_config(config: Dict[str, Any]) -> None:
    raw_alert_symbols = os.getenv("ALERT_SYMBOLS")
    if raw_alert_symbols is not None:
        symbols = [part.strip().upper() for part in raw_alert_symbols.split(",") if part.strip()]
        if symbols:
            config["symbols"] = symbols
            config["symbols_with_options"] = list(symbols)
    notifications = config.setdefault("notifications", {})
    notifications["telegram_enabled"] = env_bool(
        "ENABLE_TELEGRAM_ALERTS",
        bool(notifications.get("telegram_enabled", False)),
    )
    raw_telegram_alert_types = os.getenv("TELEGRAM_ALERT_TYPES")
    if raw_telegram_alert_types is not None:
        notifications["telegram_alert_types"] = [
            part.strip().upper()
            for part in raw_telegram_alert_types.split(",")
            if part.strip()
        ]
    notifications["telegram_aapl_only"] = env_bool(
        "TELEGRAM_AAPL_ONLY",
        bool(notifications.get("telegram_aapl_only", True)),
    )
    notifications["telegram_send_test_on_start"] = env_bool(
        "TELEGRAM_SEND_TEST_ON_START",
        bool(notifications.get("telegram_send_test_on_start", False)),
    )
    notifications["telegram_timeout_seconds"] = env_int(
        "TELEGRAM_TIMEOUT_SECONDS",
        int(notifications.get("telegram_timeout_seconds", 8)),
    )
    notifications["openai_alert_formatter_enabled"] = env_bool(
        "ENABLE_OPENAI_ALERT_FORMATTER",
        bool(notifications.get("openai_alert_formatter_enabled", True)),
    )
    notifications["openai_alert_formatter_style"] = os.getenv(
        "OPENAI_ALERT_FORMATTER_STYLE",
        str(notifications.get("openai_alert_formatter_style", "section")),
    ).strip().lower() or "section"
    notifications["openai_alert_formatter_fallback"] = env_bool(
        "OPENAI_ALERT_FORMATTER_FALLBACK",
        bool(notifications.get("openai_alert_formatter_fallback", True)),
    )
    notifications["openai_alert_formatter_max_chars"] = env_int(
        "OPENAI_ALERT_FORMATTER_MAX_CHARS",
        int(notifications.get("openai_alert_formatter_max_chars", 900)),
    )

    market_data = config.setdefault("market_data", {})
    market_data["stock_feed"] = normalize_feed(
        os.getenv("ALPACA_STOCK_FEED", market_data.get("stock_feed", "sip")),
        "sip",
    )

    options = config.setdefault("options", {})
    options["feed"] = normalize_feed(
        os.getenv("ALPACA_OPTIONS_FEED", options.get("feed", "opra")),
        "opra",
    )
    options["allow_indicative_fallback"] = env_bool(
        "ALPACA_ALLOW_INDICATIVE_OPTIONS_FALLBACK",
        bool(options.get("allow_indicative_fallback", True)),
    )
    options["max_quote_age_seconds"] = env_int(
        "MAX_OPTION_QUOTE_AGE_SECONDS",
        int(options.get("max_quote_age_seconds", 60)),
    )
    news_context = config.setdefault("news_context", {})
    news_context["enabled"] = env_bool(
        "ENABLE_NEWS_CONTEXT_LAYER",
        bool(news_context.get("enabled", False)),
    )
    news_context["lookback_minutes"] = env_int(
        "NEWS_LOOKBACK_MINUTES",
        int(news_context.get("lookback_minutes", 120)),
    )
    news_context["context_only"] = env_bool(
        "NEWS_CONTEXT_ONLY",
        bool(news_context.get("context_only", True)),
    )
    raw_news_symbols = os.getenv("NEWS_WATCH_SYMBOLS")
    if raw_news_symbols is not None:
        news_context["watch_symbols"] = [
            part.strip().upper()
            for part in raw_news_symbols.split(",")
            if part.strip()
        ]

    structure = config.setdefault("market_structure_engines", {})
    structure["enable_support_resistance_engine"] = env_bool(
        "ENABLE_SUPPORT_RESISTANCE_ENGINE",
        bool(structure.get("enable_support_resistance_engine", True)),
    )
    structure["enable_supply_demand_engine"] = env_bool(
        "ENABLE_SUPPLY_DEMAND_ENGINE",
        bool(structure.get("enable_supply_demand_engine", True)),
    )
    structure["enable_dashboard"] = env_bool(
        "ENABLE_MARKET_STRUCTURE_DASHBOARD",
        bool(structure.get("enable_dashboard", True)),
    )
    for env_name, key in (
        ("SUPPORT_RESISTANCE_TIMEFRAMES", "support_resistance_timeframes"),
        ("SUPPLY_DEMAND_TIMEFRAMES", "supply_demand_timeframes"),
    ):
        raw = os.getenv(env_name)
        if raw is not None:
            structure[key] = [part.strip() for part in raw.split(",") if part.strip()]
    structure["max_levels_per_timeframe"] = env_int(
        "MAX_LEVELS_PER_TIMEFRAME", int(structure.get("max_levels_per_timeframe", 3))
    )
    structure["max_zones_per_timeframe"] = env_int(
        "MAX_ZONES_PER_TIMEFRAME", int(structure.get("max_zones_per_timeframe", 3))
    )
    structure["refresh_seconds"] = env_int(
        "MARKET_STRUCTURE_REFRESH_SECONDS", int(structure.get("refresh_seconds", 15))
    )
    structure["min_level_strength"] = env_int(
        "MARKET_STRUCTURE_MIN_LEVEL_STRENGTH", int(structure.get("min_level_strength", 55))
    )
    structure["min_zone_strength"] = env_int(
        "MARKET_STRUCTURE_MIN_ZONE_STRENGTH", int(structure.get("min_zone_strength", 55))
    )
    structure["can_confirm"] = env_bool("MARKET_STRUCTURE_CAN_CONFIRM", bool(structure.get("can_confirm", True)))
    structure["can_downgrade"] = env_bool("MARKET_STRUCTURE_CAN_DOWNGRADE", bool(structure.get("can_downgrade", True)))
    structure["can_upgrade"] = env_bool("MARKET_STRUCTURE_CAN_UPGRADE", bool(structure.get("can_upgrade", False)))
    structure["enable_telegram"] = env_bool(
        "ENABLE_MARKET_STRUCTURE_TELEGRAM", bool(structure.get("enable_telegram", True))
    )
    structure["telegram_max_lines"] = env_int(
        "MARKET_STRUCTURE_TELEGRAM_MAX_LINES", int(structure.get("telegram_max_lines", 2))
    )
    decision = config.setdefault("decision_quality", {})
    decision["enable_chop_mode"] = env_bool("ENABLE_CHOP_MODE", bool(decision.get("enable_chop_mode", True)))
    decision["chop_mode_lookback_minutes"] = env_int(
        "CHOP_MODE_LOOKBACK_MINUTES", int(decision.get("chop_mode_lookback_minutes", 15))
    )
    decision["chop_mode_min_flips"] = env_int("CHOP_MODE_MIN_FLIPS", int(decision.get("chop_mode_min_flips", 2)))
    decision["chop_mode_min_mixed_alerts"] = env_int(
        "CHOP_MODE_MIN_MIXED_ALERTS", int(decision.get("chop_mode_min_mixed_alerts", 3))
    )
    decision["chop_mode_suppress_repeated_alerts"] = env_bool(
        "CHOP_MODE_SUPPRESS_REPEATED_ALERTS", bool(decision.get("chop_mode_suppress_repeated_alerts", True))
    )
    decision["chop_mode_cooldown_minutes"] = env_int(
        "CHOP_MODE_COOLDOWN_MINUTES", int(decision.get("chop_mode_cooldown_minutes", 15))
    )
    decision["chop_mode_allow_breakout_exit"] = env_bool(
        "CHOP_MODE_ALLOW_BREAKOUT_EXIT", bool(decision.get("chop_mode_allow_breakout_exit", True))
    )
    decision["enable_missed_clean_entry_label"] = env_bool(
        "ENABLE_MISSED_CLEAN_ENTRY_LABEL", bool(decision.get("enable_missed_clean_entry_label", True))
    )
    decision["missed_clean_entry_lookback_minutes"] = env_int(
        "MISSED_CLEAN_ENTRY_LOOKBACK_MINUTES", int(decision.get("missed_clean_entry_lookback_minutes", 15))
    )
    decision["missed_clean_entry_cooldown_minutes"] = env_int(
        "MISSED_CLEAN_ENTRY_COOLDOWN_MINUTES", int(decision.get("missed_clean_entry_cooldown_minutes", 15))
    )
    sweep = config.setdefault("liquidity_sweep_engine", {})
    sweep["enabled"] = env_bool("ENABLE_LIQUIDITY_SWEEP_ENGINE", bool(sweep.get("enabled", True)))
    raw_sweep_timeframes = os.getenv("LIQUIDITY_SWEEP_TIMEFRAMES")
    if raw_sweep_timeframes is not None:
        sweep["timeframes"] = [part.strip() for part in raw_sweep_timeframes.split(",") if part.strip()]
    sweep["min_confidence"] = env_int(
        "LIQUIDITY_SWEEP_MIN_CONFIDENCE", int(sweep.get("min_confidence", 55))
    )
    sweep["confirm_on_candle_close"] = env_bool(
        "LIQUIDITY_SWEEP_CONFIRM_ON_CANDLE_CLOSE", bool(sweep.get("confirm_on_candle_close", True))
    )
    sweep["watch_distance_bps"] = env_float(
        "LIQUIDITY_SWEEP_WATCH_DISTANCE_BPS", float(sweep.get("watch_distance_bps", 8))
    )
    sweep["cooldown_minutes"] = env_int(
        "LIQUIDITY_SWEEP_COOLDOWN_MINUTES", int(sweep.get("cooldown_minutes", 10))
    )
    sweep["use_supply_demand"] = env_bool(
        "LIQUIDITY_SWEEP_USE_SUPPLY_DEMAND", bool(sweep.get("use_supply_demand", True))
    )
    sweep["use_support_resistance"] = env_bool(
        "LIQUIDITY_SWEEP_USE_SUPPORT_RESISTANCE", bool(sweep.get("use_support_resistance", True))
    )
    sweep["can_confirm"] = env_bool("LIQUIDITY_SWEEP_CAN_CONFIRM", bool(sweep.get("can_confirm", True)))
    sweep["can_downgrade"] = env_bool("LIQUIDITY_SWEEP_CAN_DOWNGRADE", bool(sweep.get("can_downgrade", True)))
    sweep["can_upgrade"] = env_bool("LIQUIDITY_SWEEP_CAN_UPGRADE", bool(sweep.get("can_upgrade", False)))
    sweep["telegram_enabled"] = env_bool(
        "ENABLE_LIQUIDITY_SWEEP_TELEGRAM", bool(sweep.get("telegram_enabled", True))
    )
    sweep["telegram_watch_enabled"] = env_bool(
        "LIQUIDITY_SWEEP_TELEGRAM_WATCH_ENABLED", bool(sweep.get("telegram_watch_enabled", True))
    )
    sweep["telegram_forming_enabled"] = env_bool(
        "LIQUIDITY_SWEEP_TELEGRAM_FORMING_ENABLED", bool(sweep.get("telegram_forming_enabled", True))
    )
    sweep["telegram_confirmed_enabled"] = env_bool(
        "LIQUIDITY_SWEEP_TELEGRAM_CONFIRMED_ENABLED", bool(sweep.get("telegram_confirmed_enabled", True))
    )
    sweep["telegram_min_confidence"] = env_int(
        "LIQUIDITY_SWEEP_TELEGRAM_MIN_CONFIDENCE", int(sweep.get("telegram_min_confidence", 55))
    )
    sweep["telegram_confirmed_min_confidence"] = env_int(
        "LIQUIDITY_SWEEP_TELEGRAM_CONFIRMED_MIN_CONFIDENCE",
        int(sweep.get("telegram_confirmed_min_confidence", 65)),
    )
    sweep["telegram_cooldown_minutes"] = env_int(
        "LIQUIDITY_SWEEP_TELEGRAM_COOLDOWN_MINUTES", int(sweep.get("telegram_cooldown_minutes", 10))
    )
    sweep["telegram_max_chars"] = env_int(
        "LIQUIDITY_SWEEP_TELEGRAM_MAX_CHARS", int(sweep.get("telegram_max_chars", 900))
    )
    sweep["telegram_include_structure"] = env_bool(
        "LIQUIDITY_SWEEP_TELEGRAM_INCLUDE_STRUCTURE", bool(sweep.get("telegram_include_structure", True))
    )

    quality = config.setdefault("alert_quality", {})
    quality["sms_min_confirmation_score"] = env_int(
        "SMS_MIN_CONFIRMATION_SCORE",
        int(quality.get("sms_min_confirmation_score", 60)),
    )
    quality["sms_strong_confirmation_score"] = env_int(
        "SMS_STRONG_CONFIRMATION_SCORE",
        int(quality.get("sms_strong_confirmation_score", 70)),
    )
    quality["sms_block_choppy_market"] = env_bool(
        "SMS_BLOCK_CHOPPY_MARKET",
        bool(quality.get("sms_block_choppy_market", True)),
    )
    quality["sms_require_candle_alignment"] = env_bool(
        "SMS_REQUIRE_CANDLE_ALIGNMENT",
        bool(quality.get("sms_require_candle_alignment", True)),
    )
    quality["sms_require_no_direction_conflict"] = env_bool(
        "SMS_REQUIRE_NO_DIRECTION_CONFLICT",
        bool(quality.get("sms_require_no_direction_conflict", True)),
    )
    quality["sms_orb_dedupe_minutes"] = env_int(
        "SMS_ORB_DEDUPE_MINUTES",
        int(quality.get("sms_orb_dedupe_minutes", 15)),
    )
    quality["a_plus_min_confirmation_score"] = env_int(
        "A_PLUS_MIN_CONFIRMATION_SCORE",
        int(quality.get("a_plus_min_confirmation_score", 70)),
    )

    strategy = config.setdefault("strategy_engine", {})
    bool_map = {
        "ENABLE_STRATEGY_ENGINE": "enabled",
        "ENABLE_LIQUIDITY_SWEEP": "enable_liquidity_sweep",
        "ENABLE_VWAP_RECLAIM": "enable_vwap_reclaim",
        "ENABLE_OPENING_RANGE": "enable_opening_range",
        "ENABLE_VOLUME_QUALITY": "enable_volume_quality",
        "ENABLE_CANDLE_STRENGTH": "enable_candle_strength",
        "ENABLE_RETEST_HOLD": "enable_retest_hold",
        "ENABLE_EXTENSION_EXHAUSTION": "enable_extension_exhaustion",
        "ENABLE_RELATIVE_STRENGTH": "enable_relative_strength",
        "ENABLE_MARKET_REGIME": "enable_market_regime",
        "ENABLE_PRESSURE_SCORE": "enable_pressure_score",
    }
    int_map = {
        "MIN_STRATEGY_SCORE_TO_ALERT": "min_strategy_score_to_alert",
        "SWEEP_RECLAIM_CANDLES": "sweep_reclaim_candles",
        "OPENING_RANGE_MINUTES_PRIMARY": "opening_range_minutes_primary",
        "OPENING_RANGE_MINUTES_SECONDARY": "opening_range_minutes_secondary",
    }
    float_map = {
        "VOLUME_CONFIRM_MULTIPLIER": "volume_confirm_multiplier",
        "MAX_EXTENSION_FROM_VWAP_PCT": "max_extension_from_vwap_pct",
        "MAX_EXTENSION_FROM_EMA9_PCT": "max_extension_from_ema9_pct",
    }
    for env_name, key in bool_map.items():
        strategy[key] = env_bool(env_name, bool(strategy.get(key, True)))
    for env_name, key in int_map.items():
        strategy[key] = env_int(env_name, int(strategy.get(key, 0)))
    for env_name, key in float_map.items():
        strategy[key] = env_float(env_name, float(strategy.get(key, 0.0)))

    scenario = config.setdefault("scenario_engine", {})
    scenario["enabled"] = env_bool("ENABLE_SCENARIO_ENGINE", bool(scenario.get("enabled", True)))
    scenario["shadow_mode"] = env_bool("SCENARIO_ENGINE_SHADOW_MODE", bool(scenario.get("shadow_mode", False)))
    scenario["control_dashboard"] = env_bool(
        "SCENARIO_ENGINE_CONTROL_DASHBOARD",
        bool(scenario.get("control_dashboard", True)),
    )
    scenario["control_sms"] = env_bool("SCENARIO_ENGINE_CONTROL_SMS", bool(scenario.get("control_sms", False)))
    scenario["enable_phase3_heads_up_alerts"] = env_bool(
        "ENABLE_PHASE3_HEADS_UP_ALERTS",
        bool(scenario.get("enable_phase3_heads_up_alerts", True)),
    )
    scenario["phase3_heads_up_sms_enabled"] = env_bool(
        "PHASE3_HEADS_UP_SMS_ENABLED",
        bool(scenario.get("phase3_heads_up_sms_enabled", True)),
    )
    scenario["phase3_heads_up_min_scenario_score"] = env_int(
        "PHASE3_HEADS_UP_MIN_SCENARIO_SCORE",
        int(scenario.get("phase3_heads_up_min_scenario_score", 80)),
    )
    scenario["phase3_heads_up_min_stock_score"] = env_int(
        "PHASE3_HEADS_UP_MIN_STOCK_SCORE",
        int(scenario.get("phase3_heads_up_min_stock_score", 65)),
    )
    scenario["phase3_heads_up_min_confirmation_score"] = env_int(
        "PHASE3_HEADS_UP_MIN_CONFIRMATION_SCORE",
        int(scenario.get("phase3_heads_up_min_confirmation_score", 55)),
    )
    scenario["phase3_good_position_min_scenario_score"] = env_int(
        "PHASE3_GOOD_POSITION_MIN_SCENARIO_SCORE",
        int(scenario.get("phase3_good_position_min_scenario_score", 85)),
    )
    scenario["phase3_good_position_min_stock_score"] = env_int(
        "PHASE3_GOOD_POSITION_MIN_STOCK_SCORE",
        int(scenario.get("phase3_good_position_min_stock_score", 70)),
    )
    scenario["phase3_good_position_min_confirmation_score"] = env_int(
        "PHASE3_GOOD_POSITION_MIN_CONFIRMATION_SCORE",
        int(scenario.get("phase3_good_position_min_confirmation_score", 60)),
    )
    scenario["phase3_heads_up_dedupe_minutes"] = env_int(
        "PHASE3_HEADS_UP_DEDUPE_MINUTES",
        int(scenario.get("phase3_heads_up_dedupe_minutes", 15)),
    )
    raw_heads_up_symbols = os.getenv("PHASE3_HEADS_UP_SYMBOLS")
    if raw_heads_up_symbols is not None:
        scenario["phase3_heads_up_symbols"] = [
            part.strip().upper()
            for part in raw_heads_up_symbols.split(",")
            if part.strip()
        ]
    raw_market_context_symbols = os.getenv("MARKET_CONTEXT_SYMBOLS")
    if raw_market_context_symbols is not None:
        scenario["market_context_symbols"] = [
            part.strip().upper()
            for part in raw_market_context_symbols.split(",")
            if part.strip()
        ]
    scenario["phase3_late_warning_phone_enabled"] = env_bool(
        "PHASE3_LATE_WARNING_PHONE_ENABLED",
        bool(scenario.get("phase3_late_warning_phone_enabled", False)),
    )
    scenario["phase3_late_warning_dedupe_minutes"] = env_int(
        "PHASE3_LATE_WARNING_DEDUPE_MINUTES",
        int(scenario.get("phase3_late_warning_dedupe_minutes", 30)),
    )
    scenario["min_dashboard_score"] = env_int("SCENARIO_MIN_DASHBOARD_SCORE", int(scenario.get("min_dashboard_score", 55)))
    scenario["min_confirmed_score"] = env_int("SCENARIO_MIN_CONFIRMED_SCORE", int(scenario.get("min_confirmed_score", 70)))
    scenario["good_position_score"] = env_int("SCENARIO_GOOD_POSITION_SCORE", int(scenario.get("good_position_score", 75)))
    scenario["dedupe_minutes"] = env_int("SCENARIO_DEDUPE_MINUTES", int(scenario.get("dedupe_minutes", 10)))
    scenario["option_logic_separate_from_stock_setup"] = env_bool(
        "OPTION_LOGIC_SEPARATE_FROM_STOCK_SETUP",
        bool(scenario.get("option_logic_separate_from_stock_setup", True)),
    )
    scenario["options_do_not_hide_stock_setups"] = env_bool(
        "OPTIONS_DO_NOT_HIDE_STOCK_SETUPS",
        bool(scenario.get("options_do_not_hide_stock_setups", True)),
    )
    scenario["options_block_sms_only"] = env_bool(
        "OPTIONS_BLOCK_SMS_ONLY",
        bool(scenario.get("options_block_sms_only", True)),
    )
    scenario["opra_unavailable_allow_stock_dashboard"] = env_bool(
        "OPRA_UNAVAILABLE_ALLOW_STOCK_DASHBOARD",
        bool(scenario.get("opra_unavailable_allow_stock_dashboard", True)),
    )
    scenario["opra_unavailable_require_stronger_sms"] = env_bool(
        "OPRA_UNAVAILABLE_REQUIRE_STRONGER_SMS",
        bool(scenario.get("opra_unavailable_require_stronger_sms", True)),
    )
    scenario["sms_min_stock_setup_score"] = env_int(
        "SMS_MIN_STOCK_SETUP_SCORE",
        int(scenario.get("sms_min_stock_setup_score", 70)),
    )
    scenario["sms_min_confirmation_score"] = env_int(
        "SMS_MIN_CONFIRMATION_SCORE",
        int(scenario.get("sms_min_confirmation_score", 60)),
    )
    scenario["sms_strong_stock_setup_score"] = env_int(
        "SMS_STRONG_STOCK_SETUP_SCORE",
        int(scenario.get("sms_strong_stock_setup_score", 85)),
    )
    scenario["sms_strong_confirmation_score"] = env_int(
        "SMS_STRONG_CONFIRMATION_SCORE",
        int(scenario.get("sms_strong_confirmation_score", 70)),
    )
    scenario["sms_block_scenario_conflict"] = env_bool(
        "SMS_BLOCK_SCENARIO_CONFLICT",
        bool(scenario.get("sms_block_scenario_conflict", True)),
    )
    scenario["sms_require_good_stage"] = env_bool(
        "SMS_REQUIRE_GOOD_STAGE",
        bool(scenario.get("sms_require_good_stage", True)),
    )

    volume_quality = config.setdefault("confirmation", {}).setdefault("volume_quality", {})
    volume_quality["enabled"] = env_bool("ENABLE_VOLUME_QUALITY", bool(volume_quality.get("enabled", True)))
    volume_quality["rvol_lookback_candles"] = env_int(
        "RVOL_LOOKBACK_CANDLES",
        int(volume_quality.get("rvol_lookback_candles", 20)),
    )
    volume_quality["min_rvol_confirmation"] = env_float(
        "MIN_RVOL_CONFIRMATION",
        float(volume_quality.get("min_rvol_confirmation", 1.5)),
    )
    volume_quality["strong_rvol_confirmation"] = env_float(
        "STRONG_RVOL_CONFIRMATION",
        float(volume_quality.get("strong_rvol_confirmation", 2.0)),
    )
    volume_quality["climax_rvol_multiplier"] = env_float(
        "CLIMAX_RVOL_MULTIPLIER",
        float(volume_quality.get("climax_rvol_multiplier", 3.5)),
    )
    volume_quality["volume_exhaustion_candle_count"] = env_int(
        "VOLUME_EXHAUSTION_CANDLE_COUNT",
        int(volume_quality.get("volume_exhaustion_candle_count", 3)),
    )
    candle_strength = config.setdefault("confirmation", {}).setdefault("candle_strength", {})
    candle_strength["enabled"] = env_bool("ENABLE_CANDLE_STRENGTH", bool(candle_strength.get("enabled", True)))
    candle_strength["buyer_control_close_top_pct"] = env_float(
        "BUYER_CONTROL_CLOSE_TOP_PCT",
        float(candle_strength.get("buyer_control_close_top_pct", 25)),
    )
    candle_strength["seller_control_close_bottom_pct"] = env_float(
        "SELLER_CONTROL_CLOSE_BOTTOM_PCT",
        float(candle_strength.get("seller_control_close_bottom_pct", 25)),
    )
    candle_strength["min_body_pct_for_control"] = env_float(
        "MIN_BODY_PCT_FOR_CONTROL",
        float(candle_strength.get("min_body_pct_for_control", 45)),
    )
    candle_strength["large_wick_pct"] = env_float(
        "LARGE_WICK_PCT",
        float(candle_strength.get("large_wick_pct", 40)),
    )
    candle_strength["indecision_body_pct"] = env_float(
        "INDECISION_BODY_PCT",
        float(candle_strength.get("indecision_body_pct", 25)),
    )
    retest_hold = config.setdefault("confirmation", {}).setdefault("retest_hold", {})
    retest_hold["enabled"] = env_bool("ENABLE_RETEST_HOLD", bool(retest_hold.get("enabled", True)))
    retest_hold["retest_lookback_candles"] = env_int(
        "RETEST_LOOKBACK_CANDLES",
        int(retest_hold.get("retest_lookback_candles", 10)),
    )
    retest_hold["retest_max_distance_from_level_pct"] = env_float(
        "RETEST_MAX_DISTANCE_FROM_LEVEL_PCT",
        float(retest_hold.get("retest_max_distance_from_level_pct", 0.15)),
    )
    retest_hold["retest_confirm_candles"] = env_int(
        "RETEST_CONFIRM_CANDLES",
        int(retest_hold.get("retest_confirm_candles", 2)),
    )
    retest_hold["retest_pullback_volume_max_multiplier"] = env_float(
        "RETEST_PULLBACK_VOLUME_MAX_MULTIPLIER",
        float(retest_hold.get("retest_pullback_volume_max_multiplier", 1.2)),
    )
    extension = config.setdefault("confirmation", {}).setdefault("extension_exhaustion", {})
    extension["enabled"] = env_bool("ENABLE_EXTENSION_EXHAUSTION", bool(extension.get("enabled", True)))
    extension["max_extension_from_vwap_pct"] = env_float(
        "MAX_EXTENSION_FROM_VWAP_PCT",
        float(extension.get("max_extension_from_vwap_pct", strategy.get("max_extension_from_vwap_pct", 0.6))),
    )
    extension["max_extension_from_ema9_pct"] = env_float(
        "MAX_EXTENSION_FROM_EMA9_PCT",
        float(extension.get("max_extension_from_ema9_pct", strategy.get("max_extension_from_ema9_pct", 0.4))),
    )
    extension["max_extension_from_key_level_pct"] = env_float(
        "MAX_EXTENSION_FROM_KEY_LEVEL_PCT",
        float(extension.get("max_extension_from_key_level_pct", 0.3)),
    )
    extension["consecutive_large_candle_limit"] = env_int(
        "CONSECUTIVE_LARGE_CANDLE_LIMIT",
        int(extension.get("consecutive_large_candle_limit", 3)),
    )
    extension["do_not_chase_extension_score"] = env_int(
        "DO_NOT_CHASE_EXTENSION_SCORE",
        int(extension.get("do_not_chase_extension_score", 80)),
    )
    relative_strength = config.setdefault("confirmation", {}).setdefault("relative_strength", {})
    relative_strength["enabled"] = env_bool("ENABLE_RELATIVE_STRENGTH", bool(relative_strength.get("enabled", True)))
    relative_strength["rs_lookback_candles"] = env_int(
        "RS_LOOKBACK_CANDLES",
        int(relative_strength.get("rs_lookback_candles", 5)),
    )
    relative_strength["rs_strong_diff_pct"] = env_float(
        "RS_STRONG_DIFF_PCT",
        float(relative_strength.get("rs_strong_diff_pct", 0.20)),
    )
    relative_strength["rs_weak_diff_pct"] = env_float(
        "RS_WEAK_DIFF_PCT",
        float(relative_strength.get("rs_weak_diff_pct", -0.20)),
    )
    market_regime = config.setdefault("confirmation", {}).setdefault("market_regime", {})
    market_regime["enabled"] = env_bool("ENABLE_MARKET_REGIME", bool(market_regime.get("enabled", True)))
    market_regime["market_regime_lookback_candles"] = env_int(
        "MARKET_REGIME_LOOKBACK_CANDLES",
        int(market_regime.get("market_regime_lookback_candles", 15)),
    )
    market_regime["choppy_vwap_cross_count"] = env_int(
        "CHOPPY_VWAP_CROSS_COUNT",
        int(market_regime.get("choppy_vwap_cross_count", 3)),
    )
    market_regime["trend_min_score"] = env_int(
        "TREND_MIN_SCORE",
        int(market_regime.get("trend_min_score", 65)),
    )
    pressure = config.setdefault("confirmation", {}).setdefault("pressure_score", {})
    pressure["enabled"] = env_bool("ENABLE_PRESSURE_SCORE", bool(pressure.get("enabled", False)))
    pressure["pressure_lookback_trades"] = env_int(
        "PRESSURE_LOOKBACK_TRADES",
        int(pressure.get("pressure_lookback_trades", 50)),
    )
    pressure["large_print_multiplier"] = env_float(
        "LARGE_PRINT_MULTIPLIER",
        float(pressure.get("large_print_multiplier", 3.0)),
    )
    pressure["min_pressure_score_confirmation"] = env_int(
        "MIN_PRESSURE_SCORE_CONFIRMATION",
        int(pressure.get("min_pressure_score_confirmation", 60)),
    )
    pressure["max_spread_pct"] = env_float(
        "MAX_SPREAD_PCT",
        float(pressure.get("max_spread_pct", 0.08)),
    )
    pressure["enable_quote_imbalance"] = env_bool(
        "ENABLE_QUOTE_IMBALANCE",
        bool(pressure.get("enable_quote_imbalance", True)),
    )


# ------------------------------------------------------------
# CLI / tests
# ------------------------------------------------------------
def run_tests() -> int:
    import unittest

    class ScannerTests(unittest.TestCase):
        def make_strategy_bars(self, closes: List[float], highs: Optional[List[float]] = None, lows: Optional[List[float]] = None, volumes: Optional[List[float]] = None) -> List[Bar]:
            open_t = set_today_time_et(DEFAULT_CONFIG["market_open"]).astimezone(UTC)
            bars: List[Bar] = []
            for i, close in enumerate(closes):
                high = highs[i] if highs else close + 0.15
                low = lows[i] if lows else close - 0.15
                volume = volumes[i] if volumes else 1000
                bars.append(Bar(t=open_t + timedelta(minutes=i), o=closes[i - 1] if i else close, h=high, l=low, c=close, v=volume))
            return bars

        def strategy_summary(
            self,
            bars: List[Bar],
            levels: Optional[Dict[str, Optional[float]]] = None,
            rel_vol: float = 2.0,
            alignment: str = "ALIGNED",
            market_bars: Optional[Dict[str, List[Bar]]] = None,
            option_context: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            config = load_config(None)
            config["strategy_engine"]["volume_confirm_multiplier"] = 1.2
            return evaluate_strategy_suite(
                "TEST",
                bars,
                bars[-1],
                config,
                levels or {},
                rel_vol,
                alignment,
                market_bars,
                option_context=option_context,
            )

        def make_phase3_heads_up_scanner(self) -> EliteScanner:
            config = load_config(None)
            config["symbols"] = ["AAPL"]
            config["symbols_with_options"] = ["AAPL"]
            temp_dir = Path(tempfile.mkdtemp())
            return EliteScanner(
                config,
                MockProvider(["AAPL"]),
                DiscordNotifier(None),
                AlertWriter(temp_dir / "heads_up.csv", temp_dir / "heads_up.jsonl"),
                StateStore(temp_dir / "heads_up_state.json"),
            )

        def make_phase3_heads_up_alert(self, **overrides: Any) -> Alert:
            scenario_top = overrides.pop(
                "scenario_top",
                {
                    "scenario_name": "Pullback Holding",
                    "direction": "bullish",
                    "stage": "CONFIRMED",
                    "score": 87,
                    "invalidation_level": 100.25,
                    "invalidation_reason": "Loses VWAP/EMA9 support",
                },
            )
            alert = Alert(
                symbol=overrides.pop("symbol", "AAPL"),
                timestamp=now_utc(),
                category=overrides.pop("category", "WATCH SUSTAINED TREND UP"),
                price=overrides.pop("price", 101.0),
                fast_move_pct=overrides.pop("fast_move_pct", 0.1),
                day_move_pct=overrides.pop("day_move_pct", 0.8),
                relative_volume=overrides.pop("relative_volume", 1.2),
                direction=overrides.pop("direction", "BULLISH"),
                alert_grade=overrides.pop("alert_grade", "C"),
                alert_score=overrides.pop("alert_score", 54),
                primary_setup=overrides.pop("primary_setup", "VWAP Reclaim"),
                strategy_direction=overrides.pop("strategy_direction", "bullish"),
                strategy_confidence_score=overrides.pop("strategy_confidence_score", 64),
                risk_label=overrides.pop("risk_label", "MEDIUM"),
                confirmation_score=overrides.pop("confirmation_score", 63),
                entry_quality_label=overrides.pop("entry_quality_label", "GOOD_POSITION"),
                volume_label=overrides.pop("volume_label", "NORMAL"),
                candle_label=overrides.pop("candle_label", "BUYER_CONTROL"),
                extension_label=overrides.pop("extension_label", "NORMAL"),
                scenario_top=scenario_top,
                scenario_score=overrides.pop("scenario_score", 87),
                scenario_stage=overrides.pop("scenario_stage", "CONFIRMED"),
                scenario_direction=overrides.pop("scenario_direction", "bullish"),
                scenario_reasons=overrides.pop(
                    "scenario_reasons",
                    [
                        "Price above VWAP",
                        "Price above EMA9",
                        "EMA9 rising",
                        "Higher low forming",
                        "Pullback held logical support",
                    ],
                ),
                scenario_warnings=overrides.pop("scenario_warnings", []),
                stock_setup_score=overrides.pop("stock_setup_score", 70),
                option_feed_status=overrides.pop("option_feed_status", "INDICATIVE"),
            )
            for key, value in overrides.items():
                setattr(alert, key, value)
            return alert

        def make_phase3_heads_up_snapshot(self, stale: bool = False) -> SymbolSnapshot:
            base_time = now_utc() - timedelta(minutes=60 if stale else 1)
            bars = [
                Bar(
                    t=base_time - timedelta(minutes=11 - i),
                    o=100.0 + i * 0.05,
                    h=100.2 + i * 0.05,
                    l=99.9 + i * 0.05,
                    c=100.1 + i * 0.05,
                    v=200000,
                )
                for i in range(12)
            ]
            return SymbolSnapshot(symbol="AAPL", latest_bar=bars[-1], recent_bars=bars)

        def test_professional_alert_tier_assigns_all_five_tiers_without_changing_approvals(self) -> None:
            context = self.make_phase3_heads_up_alert(
                category="WATCH PREMARKET HIGH BREAK",
                primary_setup=None,
                scenario_top=None,
                scenario_stage=None,
                phase3_heads_up_type=None,
                watch_allowed=False,
            )
            forming = self.make_phase3_heads_up_alert(scenario_stage="FORMING", phase3_heads_up_type="EARLY_WATCH")
            confirmed = self.make_phase3_heads_up_alert(scenario_stage="CONFIRMED", phase3_heads_up_type="GOOD_POSITION")
            trade_quality = self.make_phase3_heads_up_alert(
                sms_allowed=True,
                option_tradable=True,
                option_quality="Tradable",
                scenario_stage="CONFIRMED",
            )
            risk = self.make_phase3_heads_up_alert(risk_label="DO_NOT_CHASE", scenario_stage="LATE")
            before = [(item.sms_allowed, item.watch_allowed, item.phase3_heads_up_sent) for item in (context, forming, confirmed, trade_quality, risk)]
            for item in (context, forming, confirmed, trade_quality, risk):
                assign_professional_alert_tier(item)
            self.assertEqual(
                [context.alert_tier, forming.alert_tier, confirmed.alert_tier, trade_quality.alert_tier, risk.alert_tier],
                ["CONTEXT", "SETUP_FORMING", "SETUP_CONFIRMED", "TRADE_QUALITY_WATCH", "RISK_WARNING"],
            )
            after = [(item.sms_allowed, item.watch_allowed, item.phase3_heads_up_sent) for item in (context, forming, confirmed, trade_quality, risk)]
            self.assertEqual(before, after)

        def test_professional_alert_tier_maps_phase3_stock_warning_and_normal_watch(self) -> None:
            phase3 = self.make_phase3_heads_up_alert(phase3_heads_up_sent=True, phase3_heads_up_type="GOOD_POSITION")
            stock_warning = self.make_phase3_heads_up_alert(
                phase3_heads_up_sent=True,
                phase3_heads_up_type="STOCK_ONLY_WARNING",
                risk_label="HIGH",
            )
            normal_watch = self.make_phase3_heads_up_alert(
                scenario_top=None,
                scenario_stage=None,
                phase3_heads_up_type=None,
                primary_setup="VWAP Reclaim",
                watch_allowed=True,
            )
            assign_professional_alert_tier(phase3)
            assign_professional_alert_tier(stock_warning)
            assign_professional_alert_tier(normal_watch)
            self.assertEqual(phase3.alert_tier, "SETUP_CONFIRMED")
            self.assertEqual(stock_warning.alert_tier, "RISK_WARNING")
            self.assertEqual(normal_watch.alert_tier, "SETUP_FORMING")
            self.assertEqual(phase3.alert_source, "PHASE3_HEADS_UP")
            self.assertEqual(normal_watch.alert_source, "NORMAL_WATCH")

        def test_professional_telegram_message_has_required_decision_support_fields(self) -> None:
            alert = self.make_phase3_heads_up_alert(
                phase3_heads_up_sent=True,
                phase3_heads_up_type="GOOD_POSITION",
                option_quality="Tradable",
            )
            message = professional_telegram_message(alert, "PHASE3_HEADS_UP")
            for label in (
                "AAPL WATCH ONLY",
                "Why:",
                "Market:",
                "Structure:",
                "Risk:",
                "Wait for:",
                "Invalidation:",
                "Option:",
                "Heads-up only — confirm manually. Not a buy/sell signal.",
            ):
                self.assertIn(label, message)
            self.assertNotIn("Phase 3 Early Heads-Up", message)
            self.assertLessEqual(len(message), 900)

        def test_professional_telegram_specific_risk_explains_low_setup_risk(self) -> None:
            alert = self.make_phase3_heads_up_alert(
                risk_label="LOW",
                setup_risk_label="LOW",
                market_regime="RANGE_BOUND",
                option_quality="STALE",
                confirmation_score=55,
            )
            message = professional_telegram_message(alert, "PHASE3_HEADS_UP")
            self.assertIn("AAPL RISK WARNING", message)
            self.assertIn("Risk: range-bound market, option stale, confirmation below 60", message)

        def test_phone_conclusions_cover_mixed_late_forming_context_and_trade_quality(self) -> None:
            mixed = self.make_phase3_heads_up_alert(
                setup_name="Mixed Signal",
                scenario_conflict=True,
                mixed_signal_reason="Bullish sweep but failed reclaim",
                sms_allowed=True,
                option_tradable=True,
                option_quality="TRADABLE",
            )
            late = self.make_phase3_heads_up_alert(scenario_stage="LATE", entry_quality_label="LATE")
            forming = self.make_phase3_heads_up_alert(scenario_stage="FORMING", entry_quality_label="EARLY")
            context = self.make_phase3_heads_up_alert(
                category="WATCH KEY LEVEL APPROACHING",
                scenario_top=None,
                scenario_stage=None,
                primary_setup=None,
                setup_name=None,
            )
            trade_quality = self.make_phase3_heads_up_alert(
                sms_allowed=True,
                option_tradable=True,
                option_quality="TRADABLE",
            )
            for item in (mixed, late, forming, context, trade_quality):
                apply_risk_invalidation(item)
                assign_professional_alert_tier(item)
            self.assertEqual(mixed.phone_conclusion, "MIXED / NO TRADE")
            self.assertTrue(mixed.mixed_signal_no_trade)
            self.assertNotEqual(mixed.alert_tier, "TRADE_QUALITY_WATCH")
            self.assertEqual(late.phone_conclusion, "DO NOT CHASE")
            self.assertEqual(forming.phone_conclusion, "WATCH ONLY")
            self.assertEqual(context.phone_conclusion, "CONTEXT ONLY")
            self.assertEqual(trade_quality.phone_conclusion, "TRADE QUALITY WATCH")
            mixed_message = professional_telegram_message(mixed, "PHASE3_HEADS_UP")
            self.assertTrue(mixed_message.startswith("AAPL MIXED / NO TRADE"))
            self.assertIn("Wait for:", mixed_message)
            self.assertIn("cleaner setup", mixed.phone_conclusion_reason.lower())

        def test_official_alert_type_reporting_uses_shared_resolver(self) -> None:
            config = load_config(None)
            config["notifications"]["telegram_alert_types"] = ["PHASE3_HEADS_UP", "NORMAL_SMS"]
            self.assertEqual(
                scanner_identity(config)["alert_types_enabled"],
                ["PHASE3_HEADS_UP", "STOCK_ONLY_WARNING", "NORMAL_WATCH", "NORMAL_SMS"],
            )

        def test_risk_engine_generates_bullish_and_bearish_invalidation(self) -> None:
            bullish = self.make_phase3_heads_up_alert(
                scenario_top=None,
                scenario_direction="bullish",
                strategy_levels={"vwap": 100.4, "ema9": 100.6, "recent_swing_low": 99.8},
            )
            bearish = self.make_phase3_heads_up_alert(
                price=99.0,
                direction="BEARISH",
                strategy_direction="bearish",
                scenario_direction="bearish",
                scenario_top=None,
                strategy_levels={"vwap": 99.5, "ema9": 99.3, "recent_swing_high": 100.2},
            )
            apply_risk_invalidation(bullish)
            apply_risk_invalidation(bearish)
            self.assertEqual(bullish.invalidation_level, 100.6)
            self.assertIn("loses EMA9", bullish.invalidation_reason)
            self.assertEqual(bearish.invalidation_level, 99.3)
            self.assertIn("reclaims EMA9", bearish.invalidation_reason)
            self.assertIn("confirmed close below", bullish.stop_logic_description)
            self.assertIn("confirmed close above", bearish.stop_logic_description)

        def test_risk_engine_missing_invalidation_blocks_trade_quality(self) -> None:
            alert = self.make_phase3_heads_up_alert(
                scenario_top=None,
                strategy_levels={},
                scenario_levels={},
                trigger_level=None,
                sms_allowed=True,
                option_tradable=True,
                option_quality="Tradable",
            )
            apply_risk_invalidation(alert)
            assign_professional_alert_tier(alert)
            self.assertFalse(alert.sms_allowed)
            self.assertEqual(alert.alert_tier, "SETUP_FORMING")
            self.assertIn("No clean invalidation", alert.invalidation_reason)
            self.assertIn("no clean invalidation", alert.sms_block_reason.lower())

        def test_risk_engine_late_alert_requires_pullback_and_says_do_not_chase(self) -> None:
            alert = self.make_phase3_heads_up_alert(
                scenario_stage="LATE",
                entry_quality_label="LATE",
                risk_label="DO_NOT_CHASE",
            )
            apply_risk_invalidation(alert)
            message = professional_telegram_message(alert, "PHASE3_HEADS_UP")
            self.assertEqual(alert.entry_timing_label, "DO_NOT_CHASE")
            self.assertTrue(alert.pullback_required)
            self.assertTrue(alert.do_not_chase_warning)
            self.assertTrue(message.startswith("AAPL DO NOT CHASE"))
            self.assertIn("Risk:", message)
            self.assertIn("Invalidation:", message)

        def with_env(self, values: Dict[str, str]) -> Dict[str, Optional[str]]:
            old = {key: os.environ.get(key) for key in values}
            for key, value in values.items():
                os.environ[key] = value
            return old

        def restore_env(self, old: Dict[str, Optional[str]]) -> None:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        def strategy_result(self, summary: Dict[str, Any], strategy: str, label: Optional[str] = None) -> Dict[str, Any]:
            matches = [item for item in summary["strategy_results"] if item["strategy"] == strategy]
            if label is not None:
                matches = [item for item in matches if item["label"] == label]
            self.assertTrue(matches, f"missing {strategy} {label or ''}")
            return matches[0]

        def test_pct_change(self) -> None:
            self.assertAlmostEqual(pct_change(110, 100), 10.0)
            self.assertAlmostEqual(pct_change(90, 100), -10.0)

        def test_alpaca_stock_feed_env_selects_sip(self) -> None:
            old = self.with_env({"ALPACA_STOCK_FEED": "sip"})
            try:
                config = load_config(None)
            finally:
                self.restore_env(old)
            self.assertEqual(stock_feed_from_config(config), "sip")
            provider = AlpacaProvider("key", "secret", feed=stock_feed_from_config(config))
            self.assertEqual(provider.feed, "sip")

        def test_alpaca_options_feed_env_selects_opra(self) -> None:
            old = self.with_env({"ALPACA_OPTIONS_FEED": "opra"})
            try:
                config = load_config(None)
            finally:
                self.restore_env(old)
            self.assertEqual(options_feed_from_config(config), "opra")

        def test_opra_agreement_error_falls_back_to_indicative_when_enabled(self) -> None:
            class FakeResponse:
                def __init__(self, status_code: int, body: Dict[str, Any], text: str = "") -> None:
                    self.status_code = status_code
                    self._body = body
                    self.text = text

                def json(self) -> Dict[str, Any]:
                    return self._body

            class FakeSession:
                def __init__(self) -> None:
                    self.calls: List[Dict[str, Any]] = []
                    self.headers: Dict[str, str] = {}

                def get(self, url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 0) -> FakeResponse:
                    self.calls.append({"url": url, "params": params or {}, "timeout": timeout})
                    if "/stocks/bars/latest" in url:
                        return FakeResponse(200, {"bars": {"AAPL": {"t": now_utc().isoformat(), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}}})
                    feed = (params or {}).get("feed")
                    if feed == "opra":
                        return FakeResponse(403, {}, '{"message":"OPRA agreement is not signed"}')
                    return FakeResponse(200, {"snapshots": {}})

            config = load_config(None)
            config["options"]["feed"] = "opra"
            config["options"]["allow_indicative_fallback"] = True
            provider = AlpacaProvider("key", "secret", feed="sip")
            fake = FakeSession()
            provider.session = fake  # type: ignore[assignment]
            status = provider.check_market_data_status(config, symbol="AAPL")
            self.assertEqual(status["stock_feed_status"], "SIP")
            self.assertEqual(status["opra_status"], "agreement missing")
            self.assertEqual(status["options_feed_status"], "INDICATIVE")
            self.assertIn("OPRA agreement not signed", status["feed_warning"])
            self.assertEqual([call["params"].get("feed") for call in fake.calls if "/options/" in call["url"]], ["opra", "indicative"])

        def test_strategy_clean_bullish_breakout(self) -> None:
            bars = self.make_strategy_bars([99.2, 99.5, 99.8, 100.1, 100.4, 101.2], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars, {"pmh": 101.0})
            result = self.strategy_result(summary, "breakout")
            self.assertTrue(result["active"])
            self.assertEqual(result["direction"], "bullish")
            self.assertIn(result["label"], {"Clean Breakout", "Do Not Chase"})

        def test_strategy_clean_bearish_breakdown(self) -> None:
            bars = self.make_strategy_bars([101.2, 100.8, 100.4, 100.0, 99.6, 98.8], volumes=[1000, 1000, 1000, 1100, 1200, 3500])
            summary = self.strategy_summary(bars, {"pml": 99.0})
            result = self.strategy_result(summary, "breakout")
            self.assertTrue(result["active"])
            self.assertEqual(result["direction"], "bearish")

        def test_strategy_weak_breakout_with_no_volume(self) -> None:
            bars = self.make_strategy_bars([99.5, 99.8, 100.0, 100.2, 100.4, 100.7], volumes=[2000, 2000, 2000, 2000, 2000, 600])
            summary = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=0.3)
            result = self.strategy_result(summary, "breakout")
            self.assertTrue(result["active"])
            self.assertIn("volume", " ".join(result["warnings"]).lower())
            self.assertLess(result["score"], 60)

        def test_strategy_breakout_do_not_chase_condition(self) -> None:
            bars = self.make_strategy_bars([99.0, 99.2, 99.4, 99.7, 100.0, 104.0], volumes=[1000, 1000, 1000, 1000, 1200, 4000])
            summary = self.strategy_summary(bars, {"pmh": 100.5})
            self.assertEqual(summary["risk_label"], "DO_NOT_CHASE")

        def test_strategy_bullish_liquidity_sweep_reclaim(self) -> None:
            bars = self.make_strategy_bars(
                [100.2, 99.8, 99.4, 98.9, 99.2, 99.7],
                lows=[100.0, 99.6, 99.2, 98.5, 98.8, 99.1],
                volumes=[1000, 1000, 1000, 2200, 2500, 3600],
            )
            summary = self.strategy_summary(bars, {"pml": 99.0})
            result = self.strategy_result(summary, "liquidity_sweep")
            self.assertEqual(result["label"], "Bullish Liquidity Sweep Reclaim")

        def test_strategy_bearish_liquidity_sweep_rejection(self) -> None:
            bars = self.make_strategy_bars(
                [99.8, 100.2, 100.6, 101.2, 100.8, 100.3],
                highs=[100.0, 100.4, 100.8, 101.6, 101.2, 100.7],
                volumes=[1000, 1000, 1000, 2300, 2600, 3600],
            )
            summary = self.strategy_summary(bars, {"pmh": 101.0})
            result = self.strategy_result(summary, "liquidity_sweep")
            self.assertEqual(result["label"], "Bearish Liquidity Sweep Rejection")

        def test_strategy_sweep_without_reclaim_should_not_alert(self) -> None:
            bars = self.make_strategy_bars([100.0, 99.6, 99.2, 98.8, 98.7, 98.6], lows=[99.8, 99.4, 99.0, 98.5, 98.4, 98.3])
            summary = self.strategy_summary(bars, {"pml": 99.0})
            result = self.strategy_result(summary, "liquidity_sweep")
            self.assertFalse(result["active"])

        def test_strategy_reclaim_weak_volume_lowers_score(self) -> None:
            bars = self.make_strategy_bars(
                [100.2, 99.8, 99.4, 98.9, 99.2, 99.7],
                lows=[100.0, 99.6, 99.2, 98.5, 98.8, 99.1],
                volumes=[2000, 2000, 2000, 2000, 2000, 600],
            )
            summary = self.strategy_summary(bars, {"pml": 99.0}, rel_vol=0.3)
            result = self.strategy_result(summary, "liquidity_sweep")
            self.assertTrue(result["active"])
            self.assertLess(result["score"], 70)

        def test_strategy_vwap_reclaim(self) -> None:
            bars = self.make_strategy_bars([100.0, 99.8, 99.6, 99.4, 99.2, 100.4], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars)
            result = self.strategy_result(summary, "vwap")
            self.assertEqual(result["label"], "VWAP Reclaim")

        def test_strategy_vwap_loss(self) -> None:
            bars = self.make_strategy_bars([100.0, 100.3, 100.5, 100.7, 100.9, 99.6], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars)
            result = self.strategy_result(summary, "vwap")
            self.assertEqual(result["label"], "VWAP Loss")

        def test_strategy_vwap_rejection(self) -> None:
            bars = self.make_strategy_bars(
                [100.0, 99.7, 99.5, 99.4, 99.45, 99.2],
                highs=[100.1, 99.8, 99.7, 99.6, 100.0, 99.8],
                volumes=[1000, 1000, 1000, 1000, 1600, 2600],
            )
            summary = self.strategy_summary(bars)
            result = self.strategy_result(summary, "vwap")
            self.assertEqual(result["label"], "VWAP Rejection")

        def test_strategy_price_near_vwap_without_confirmation_should_not_alert(self) -> None:
            bars = self.make_strategy_bars([100.0, 100.1, 100.0, 100.1, 100.0, 100.05], volumes=[1000, 1000, 1000, 1000, 1000, 1000])
            summary = self.strategy_summary(bars, rel_vol=1.0)
            result = self.strategy_result(summary, "vwap")
            self.assertFalse(result["active"])

        def test_strategy_five_minute_orb_long(self) -> None:
            bars = self.make_strategy_bars([100.0, 100.2, 100.4, 100.3, 100.5, 101.2], highs=[100.2, 100.4, 100.6, 100.5, 100.7, 101.4], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars)
            result = self.strategy_result(summary, "opening_range", "5-Min ORB Long")
            self.assertTrue(result["active"])

        def test_strategy_fifteen_minute_orb_forms_correctly(self) -> None:
            closes = [100 + i * 0.03 for i in range(15)] + [101.2]
            highs = [100.6 for _ in range(15)] + [101.4]
            lows = [99.5 for _ in range(15)] + [100.9]
            bars = self.make_strategy_bars(closes, highs=highs, lows=lows, volumes=[1000] * 15 + [3500])
            summary = self.strategy_summary(bars)
            result = self.strategy_result(summary, "opening_range", "15-Min ORB Long")
            self.assertTrue(result["active"])

        def test_strategy_orb_does_not_alert_before_range_completes(self) -> None:
            bars = self.make_strategy_bars([100.0, 100.2, 101.0], highs=[100.1, 100.3, 101.2])
            summary = self.strategy_summary(bars)
            result = self.strategy_result(summary, "opening_range", "5-Min OR Not Formed")
            self.assertFalse(result["active"])

        def test_strategy_orb_short_with_volume_confirmation(self) -> None:
            bars = self.make_strategy_bars([100.5, 100.3, 100.2, 100.1, 100.0, 98.9], lows=[100.2, 100.1, 99.9, 99.8, 99.7, 98.7], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars)
            result = self.strategy_result(summary, "opening_range", "5-Min ORB Short")
            self.assertTrue(result["active"])
            self.assertGreaterEqual(result["score"], 60)

        def test_strategy_orb_fakeout_warning(self) -> None:
            bars = self.make_strategy_bars([100.0, 100.2, 100.4, 100.3, 100.5, 103.0], highs=[100.2, 100.4, 100.6, 100.5, 100.7, 103.2], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars)
            result = self.strategy_result(summary, "opening_range", "5-Min ORB Long")
            self.assertTrue(any("Fakeout" in warning for warning in result["warnings"]))

        def test_strategy_multiple_active_increases_confidence(self) -> None:
            bars = self.make_strategy_bars([100.0, 99.8, 99.6, 99.4, 99.2, 101.2], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars, {"pmh": 100.8})
            active = [item for item in summary["strategy_results"] if item["active"]]
            self.assertGreaterEqual(len(active), 2)
            self.assertGreaterEqual(summary["confidence_score"], max(item["score"] for item in active))

        def test_strategy_contradicting_strategies_lower_confidence(self) -> None:
            bars = self.make_strategy_bars([100.0, 99.8, 99.6, 99.4, 99.2, 100.4], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars, {"pmh": 101.0, "pml": 99.0})
            manual_active = [
                {"score": 80, "direction": "bullish", "label": "VWAP Reclaim"},
                {"score": 75, "direction": "bearish", "label": "Bearish Liquidity Sweep Rejection"},
            ]
            warnings: List[str] = []
            from strategies.scoring import _combined_score
            score = _combined_score(manual_active, warnings)
            self.assertLess(score, 95)
            self.assertIn("Contradicting strategies are active", warnings)

        def test_phase3_bullish_trend_continuation_scenario(self) -> None:
            bars = self.make_strategy_bars(
                [100.0, 100.2, 100.4, 100.6, 100.85, 101.2],
                highs=[100.15, 100.35, 100.55, 100.75, 100.95, 101.35],
                lows=[99.9, 100.05, 100.2, 100.35, 100.55, 100.9],
                volumes=[1200, 1300, 1400, 1500, 1600, 2600],
            )
            summary = self.strategy_summary(bars, {"vwap": 100.25}, rel_vol=2.1, alignment="ALIGNED")
            self.assertIn(
                summary["scenario_top"]["scenario_name"],
                {"Bullish Trend Continuation", "Breakout Continuation", "Bullish VWAP/EMA Reclaim Continuation"},
            )
            self.assertEqual(summary["scenario_top"]["direction"], "bullish")
            self.assertIn(summary["scenario_top"]["stage"], {"CONFIRMED", "GOOD_POSITION", "FORMING"})

        def test_phase3_bearish_trend_continuation_scenario(self) -> None:
            bars = self.make_strategy_bars(
                [100.8, 100.6, 100.4, 100.2, 99.95, 99.7],
                highs=[100.95, 100.75, 100.55, 100.35, 100.1, 99.85],
                lows=[100.7, 100.45, 100.25, 100.05, 99.8, 99.55],
                volumes=[1200, 1300, 1400, 1500, 1600, 2600],
            )
            summary = self.strategy_summary(bars, {"vwap": 100.5}, rel_vol=2.1, alignment="ALIGNED")
            self.assertIn(
                summary["scenario_top"]["scenario_name"],
                {"Bearish Trend Continuation", "Breakdown Continuation", "Bearish VWAP/EMA Rejection Continuation"},
            )
            self.assertEqual(summary["scenario_top"]["direction"], "bearish")
            self.assertIn(summary["scenario_top"]["stage"], {"CONFIRMED", "GOOD_POSITION", "FORMING"})

        def test_phase3_scenario_alert_eligibility_and_would_sms(self) -> None:
            bars = self.make_strategy_bars(
                [100.0, 100.05, 100.1, 100.15, 100.2, 100.28],
                highs=[100.08, 100.14, 100.19, 100.24, 100.3, 100.36],
                lows=[99.95, 100.0, 100.05, 100.1, 100.15, 100.22],
                volumes=[1200, 1300, 1400, 1500, 1700, 2600],
            )
            summary = evaluate_strategy_suite(
                "TEST",
                bars,
                bars[-1],
                load_config(None),
                {"vwap": 100.05, "ema9": 100.02, "pmh": 100.25},
                2.2,
                "ALIGNED",
                option_context={"option_feed_status": "OPRA", "option_tradable": True, "option_tradability_score": 90},
                phase1_summary={"confidence_score": 84, "direction": "bullish"},
                phase2_summary={
                    "confirmation_score": 72,
                    "candle_label": "BUYER_CONTROL",
                    "volume_label": "STRONG",
                    "market_regime": "BULL_TREND",
                    "entry_quality_label": "GOOD_POSITION",
                },
            )
            self.assertTrue(summary["scenario_alert_eligible"])
            self.assertTrue(summary["scenario_would_sms"])
            self.assertEqual(summary["scenario_alert_tier"], "WOULD_SMS")
            self.assertEqual(summary["scenario_sms_block_reason"], "")
            self.assertEqual(summary["scenario_alert_block_reason"], "")

        def test_phase3_scenario_sms_blocks_without_option_support(self) -> None:
            bars = self.make_strategy_bars(
                [100.0, 100.05, 100.1, 100.15, 100.2, 100.28],
                highs=[100.08, 100.14, 100.19, 100.24, 100.3, 100.36],
                lows=[99.95, 100.0, 100.05, 100.1, 100.15, 100.22],
                volumes=[1200, 1300, 1400, 1500, 1700, 2600],
            )
            summary = evaluate_strategy_suite(
                "TEST",
                bars,
                bars[-1],
                load_config(None),
                {"vwap": 100.05, "ema9": 100.02, "pmh": 100.25},
                2.2,
                "ALIGNED",
                option_context={"option_feed_status": "UNAVAILABLE", "option_tradable": False, "option_tradability_score": 40},
                phase1_summary={"confidence_score": 84, "direction": "bullish"},
                phase2_summary={
                    "confirmation_score": 72,
                    "candle_label": "BUYER_CONTROL",
                    "volume_label": "STRONG",
                    "market_regime": "BULL_TREND",
                    "entry_quality_label": "GOOD_POSITION",
                },
            )
            self.assertTrue(summary["scenario_alert_eligible"])
            self.assertFalse(summary["scenario_would_sms"])
            self.assertEqual(summary["scenario_alert_tier"], "DASHBOARD_ALERT")
            self.assertIn("option", summary["scenario_sms_block_reason"].lower())
            self.assertIn("option", summary["scenario_alert_block_reason"].lower())

        def test_phase3_late_phase2_downgrades_good_position(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.8, 100.1, 100.4, 100.7, 101.1],
                highs=[99.7, 100.0, 100.3, 100.6, 100.9, 101.3],
                lows=[99.3, 99.6, 99.9, 100.2, 100.5, 100.9],
                volumes=[1000, 1000, 1400, 1600, 1800, 2600],
            )
            summary = evaluate_strategy_suite(
                "TEST",
                bars,
                bars[-1],
                load_config(None),
                {"vwap": 100.0, "ema9": 99.95, "pmh": 100.9},
                2.0,
                "ALIGNED",
                phase1_summary={"confidence_score": 86, "direction": "bullish"},
                phase2_summary={
                    "confirmation_score": 74,
                    "candle_label": "BUYER_CONTROL",
                    "volume_label": "STRONG",
                    "market_regime": "BULL_TREND",
                    "entry_quality_label": "LATE",
                },
            )
            self.assertNotEqual(summary["scenario_stage"], "GOOD_POSITION")
            self.assertIn("Stage downgraded because Phase 2 entry quality is LATE", summary["scenario_warnings"])
            self.assertIn(summary["scenario_stage"], {"LATE", "CONFIRMED", "FORMING"})
            self.assertFalse(summary["scenario_would_sms"])

        def test_phase3_do_not_chase_forces_blocked_stage(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.8, 100.1, 100.4, 100.7, 101.1],
                highs=[99.7, 100.0, 100.3, 100.6, 100.9, 101.3],
                lows=[99.3, 99.6, 99.9, 100.2, 100.5, 100.9],
                volumes=[1000, 1000, 1400, 1600, 1800, 2600],
            )
            summary = evaluate_strategy_suite(
                "TEST",
                bars,
                bars[-1],
                load_config(None),
                {"vwap": 100.0, "ema9": 99.95, "pmh": 100.9},
                2.0,
                "ALIGNED",
                phase1_summary={"confidence_score": 86, "direction": "bullish"},
                phase2_summary={
                    "confirmation_score": 78,
                    "candle_label": "BUYER_CONTROL",
                    "volume_label": "STRONG",
                    "market_regime": "BULL_TREND",
                    "entry_quality_label": "DO_NOT_CHASE",
                },
            )
            self.assertEqual(summary["scenario_stage"], "DO_NOT_CHASE")
            self.assertEqual(summary["scenario_alert_tier"], "BLOCKED")
            self.assertFalse(summary["scenario_would_sms"])
            self.assertIn("DO_NOT_CHASE", summary["scenario_sms_block_reason"])

        def test_phase3_failed_reclaim_is_labeled_as_failed_not_bullish_continuation(self) -> None:
            bars = self.make_strategy_bars(
                [100.2, 99.9, 99.6, 99.2, 99.5, 99.4],
                highs=[100.3, 100.0, 99.8, 99.6, 100.1, 100.0],
                lows=[100.0, 99.7, 99.3, 98.9, 99.1, 99.0],
                volumes=[1200, 1200, 1200, 2600, 1800, 1600],
            )
            summary = evaluate_strategy_suite(
                "TEST",
                bars,
                bars[-1],
                load_config(None),
                {"vwap": 99.5, "ema9": 99.55, "recent_high": 100.0},
                0.9,
                "OPPOSED",
                phase1_summary={"confidence_score": 82, "direction": "bullish"},
                phase2_summary={
                    "confirmation_score": 54,
                    "candle_label": "REJECTION",
                    "volume_label": "WEAK",
                    "market_regime": "CHOPPY",
                    "entry_quality_label": "EARLY",
                },
            )
            self.assertEqual(summary["scenario_top"]["scenario_name"], "Failed VWAP/EMA Reclaim")
            self.assertNotEqual(summary["scenario_top"]["scenario_name"], "Bullish VWAP/EMA Reclaim Continuation")
            self.assertFalse(summary["scenario_would_sms"])
            self.assertIn("Bullish reclaim rejected", summary["scenario_alert_block_reason"])
            self.assertIn("current direction does not match top scenario", summary["scenario_alert_block_reason"].lower())
            self.assertIn(summary["scenario_top"]["stage"], {"WATCHING", "FORMING"})

        def test_phase3_alert_tier_helper_covers_watch_and_blocked(self) -> None:
            from strategies.scenario.scenario_engine import _phase3_alert_tier, _phase3_alert_block_reason
            from strategies.scenario.scenario_types import ScenarioResult

            watch = ScenarioResult(scenario_name="Watch", direction="bullish", stage="FORMING", score=45, entry_quality_label="EARLY")
            blocked = ScenarioResult(scenario_name="Blocked", direction="bearish", stage="LATE", score=90, entry_quality_label="LATE", risk_label="HIGH")
            self.assertEqual(
                _phase3_alert_tier(
                    top=watch,
                    scenario_conflict=False,
                    direction_conflict=False,
                    phase2_candle_label="BUYER_CONTROL",
                    phase2_confirmation_score=55,
                    phase2_entry_quality="EARLY",
                    extended_from_vwap=False,
                    extended_from_ema=False,
                    would_sms=False,
                ),
                "WATCH_ONLY",
            )
            self.assertEqual(
                _phase3_alert_tier(
                    top=blocked,
                    scenario_conflict=True,
                    direction_conflict=True,
                    phase2_candle_label="SELLER_CONTROL",
                    phase2_confirmation_score=55,
                    phase2_entry_quality="LATE",
                    extended_from_vwap=True,
                    extended_from_ema=False,
                    would_sms=False,
                ),
                "BLOCKED",
            )
            self.assertIn(
                "current direction does not match top scenario",
                _phase3_alert_block_reason(
                    tier="BLOCKED",
                    top=blocked,
                    scenario_conflict=True,
                    direction_conflict=True,
                    phase2_candle_label="SELLER_CONTROL",
                    phase2_confirmation_score=55,
                    phase2_entry_quality="LATE",
                    extended_from_vwap=True,
                    extended_from_ema=False,
                    stage_downgrade_reason="Stage downgraded because Phase 2 entry quality is LATE",
                ).lower(),
            )

        def test_phase3_stock_setup_reason_explains_low_score(self) -> None:
            from strategies.scenario.scenario_engine import _stock_setup_score_reason
            from strategies.scenario.scenario_types import ScenarioResult

            active = [
                ScenarioResult(
                    scenario_name="Bullish Trend Continuation",
                    direction="bullish",
                    score=28,
                    reasons=["Price is below VWAP"],
                    warnings=["Volume confirmation is weak"],
                )
            ]
            reason = _stock_setup_score_reason(active[0], active, 16, "GOOD_POSITION", active[0].warnings)
            self.assertIn("below vwap", reason.lower())
            self.assertIn("good_position", reason.lower())

        def test_phase3_heads_up_aapl_pullback_holding_confirmed_is_eligible(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert()
            snap = self.make_phase3_heads_up_snapshot()
            scanner.evaluate_phase3_heads_up(alert, snap, "Fresh")
            self.assertTrue(alert.phase3_heads_up_eligible)
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertFalse(alert.sms_allowed)
            self.assertEqual(alert.alert_grade, "C")
            self.assertEqual(alert.phase3_heads_up_type, "GOOD_POSITION")
            message = phase3_heads_up_message(alert)
            self.assertIn("AAPL Phase 3 Heads-Up", message)
            self.assertIn("Bullish Pullback Holding — CONFIRMED", message)
            self.assertIn("Heads-up only — confirm on chart", message)

        def test_phase3_heads_up_forming_is_early_watch(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                scenario_stage="FORMING",
                scenario_score=84,
                stock_setup_score=70,
                confirmation_score=58,
                entry_quality_label="EARLY",
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "FORMING", "score": 84},
            )
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertEqual(alert.phase3_heads_up_type, "EARLY_WATCH")
            self.assertIn("watch chart, do not enter yet", phase3_heads_up_message(alert))

        def test_phase3_heads_up_legacy_conflict_warns_but_does_not_block(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(direction="BEARISH", strategy_direction="bearish")
            snap = self.make_phase3_heads_up_snapshot()
            scanner.evaluate_phase3_heads_up(alert, snap, "Fresh")
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertIn("Legacy/Phase 2 conflict present — confirm manually.", alert.scenario_warnings)
            self.assertIn("Legacy/Phase 2 conflict present", phase3_heads_up_message(alert))

        def test_phase3_heads_up_do_not_chase_sends_stock_only_warning(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(risk_label="DO_NOT_CHASE")
            snap = self.make_phase3_heads_up_snapshot()
            scanner.evaluate_phase3_heads_up(alert, snap, "Fresh")
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertTrue(alert.stock_only_heads_up_allowed)
            self.assertEqual(alert.phase3_heads_up_type, "STOCK_ONLY_WARNING")
            self.assertIn("Do Not Chase — watch only.", alert.scenario_warnings)

        def test_phase3_heads_up_high_risk_blocks(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(risk_label="HIGH")
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertFalse(alert.phase3_heads_up_sent)
            self.assertEqual(alert.phase3_heads_up_block_reason, "Blocked: risk is HIGH")

        def test_phase3_heads_up_low_confirmation_blocks(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(confirmation_score=54)
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertFalse(alert.phase3_heads_up_sent)
            self.assertEqual(alert.phase3_heads_up_block_reason, "Blocked: confirmation below 55")

        def test_phase3_heads_up_late_entry_sends_stock_only_warning(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(entry_quality_label="LATE")
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertTrue(alert.stock_only_heads_up_allowed)
            self.assertEqual(alert.phase3_heads_up_type, "STOCK_ONLY_WARNING")

        def test_phase3_heads_up_scenario_conflict_blocks(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(scenario_conflict=True)
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertFalse(alert.phase3_heads_up_sent)
            self.assertEqual(alert.phase3_heads_up_block_reason, "Blocked: scenario conflict")

        def test_phase3_heads_up_only_configured_symbol_sends(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(symbol="SPY")
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertFalse(alert.phase3_heads_up_sent)
            self.assertIn("symbol not enabled", alert.phase3_heads_up_block_reason)

        def test_phase3_heads_up_late_stage_blocks(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                scenario_stage="LATE",
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "LATE", "score": 87},
            )
            snap = self.make_phase3_heads_up_snapshot()
            scanner.evaluate_phase3_heads_up(alert, snap, "Fresh")
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertTrue(alert.stock_only_heads_up_allowed)
            self.assertEqual(alert.phase3_heads_up_type, "WATCH_ONLY_LATE_MOVE")
            self.assertIn("Late warning — do not chase.", alert.scenario_warnings)
            self.assertIn("not a buy/sell signal", phase3_heads_up_message(alert))

        def test_stock_only_phase3_late_pullback_holding_sends_warning(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                scenario_stage="LATE",
                scenario_score=83,
                stock_setup_score=63,
                confirmation_score=60,
                entry_quality_label="LATE",
                option_quality="Stale quote",
                option_tradable=False,
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "LATE", "score": 83},
            )
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh", {})
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertEqual(alert.phase3_heads_up_final_decision, "TELEGRAM_ATTEMPTED")
            self.assertTrue(alert.market_context_missing_warning)
            self.assertTrue(alert.option_stale_did_not_block_heads_up)
            message = phase3_heads_up_message(alert)
            self.assertIn("AAPL Phase 3 Watch-Only Heads-Up", message)
            self.assertIn("LATE / DO NOT CHASE", message)
            self.assertIn("not trade-ready", message)
            self.assertIn("Option quote stale/missing", message)

        def test_stock_only_phase3_do_not_chase_is_warning_only(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                risk_label="DO_NOT_CHASE",
                entry_quality_label="DO_NOT_CHASE",
                scenario_stage="DO_NOT_CHASE",
                scenario_score=90,
                confirmation_score=65,
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "DO_NOT_CHASE", "score": 90},
            )
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertFalse(alert.sms_allowed)
            self.assertIn("Do Not Chase — watch only.", alert.scenario_warnings)

        def test_watch_only_late_move_with_aligned_context_sends_telegram_heads_up(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                risk_label="HIGH",
                entry_quality_label="LATE",
                scenario_stage="LATE",
                scenario_score=83,
                stock_setup_score=51,
                confirmation_score=50,
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "LATE", "score": 83},
            )
            scanner.evaluate_phase3_heads_up(
                alert,
                self.make_phase3_heads_up_snapshot(),
                "Fresh",
                {"SPY": "BULLISH", "QQQ": "BULLISH"},
            )
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertTrue(alert.stock_only_heads_up_allowed)
            self.assertTrue(alert.watch_only_late_move)
            self.assertEqual(alert.phase3_heads_up_type, "WATCH_ONLY_LATE_MOVE")
            self.assertEqual(alert.phase3_heads_up_final_decision, "TELEGRAM_ATTEMPTED")
            self.assertFalse(alert.sms_allowed)
            message = phase3_heads_up_message(alert)
            self.assertIn("WATCH ONLY — LATE / DO NOT CHASE", message)
            self.assertIn("This is not trade-ready.", message)
            self.assertIn("Wait for pullback/retest.", message)

        def test_watch_only_late_move_scenario_conflict_blocks(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                scenario_conflict=True,
                risk_label="HIGH",
                entry_quality_label="LATE",
                scenario_stage="LATE",
                scenario_score=83,
                stock_setup_score=51,
                confirmation_score=50,
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "LATE", "score": 83},
            )
            scanner.evaluate_phase3_heads_up(
                alert,
                self.make_phase3_heads_up_snapshot(),
                "Fresh",
                {"SPY": "BULLISH", "QQQ": "BULLISH"},
            )
            self.assertFalse(alert.phase3_heads_up_sent)
            self.assertFalse(alert.watch_only_late_move)

        def test_do_not_chase_scenario_sends_watch_only_warning(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                direction="NEUTRAL",
                strategy_direction="neutral",
                scenario_direction="neutral",
                risk_label="DO_NOT_CHASE",
                entry_quality_label="DO_NOT_CHASE",
                extension_label="VERY_EXTENDED",
                scenario_stage="DO_NOT_CHASE",
                scenario_score=58,
                stock_setup_score=7,
                confirmation_score=47,
                scenario_top={
                    "scenario_name": "Do Not Chase",
                    "direction": "neutral",
                    "stage": "DO_NOT_CHASE",
                    "score": 58,
                    "do_not_chase": True,
                },
            )
            scanner.evaluate_phase3_heads_up(
                alert,
                self.make_phase3_heads_up_snapshot(),
                "Fresh",
                {"SPY": "BULLISH", "QQQ": "BULLISH"},
            )
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertTrue(alert.do_not_chase_watch)
            self.assertEqual(alert.phase3_heads_up_type, "DO_NOT_CHASE_WATCH")
            self.assertFalse(alert.sms_allowed)
            message = phase3_heads_up_message(alert)
            self.assertIn("AAPL Watch-Only Warning", message)
            self.assertIn("Not a buy/sell signal.", message)

        def test_do_not_chase_watch_duplicate_is_deduped(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            kwargs = {
                "direction": "NEUTRAL",
                "strategy_direction": "neutral",
                "scenario_direction": "neutral",
                "risk_label": "DO_NOT_CHASE",
                "entry_quality_label": "DO_NOT_CHASE",
                "extension_label": "VERY_EXTENDED",
                "scenario_stage": "DO_NOT_CHASE",
                "scenario_score": 58,
                "stock_setup_score": 7,
                "confirmation_score": 47,
                "scenario_top": {
                    "scenario_name": "Do Not Chase",
                    "direction": "neutral",
                    "stage": "DO_NOT_CHASE",
                    "score": 58,
                    "do_not_chase": True,
                },
            }
            context = {"SPY": "BULLISH", "QQQ": "BULLISH"}
            first = self.make_phase3_heads_up_alert(**kwargs)
            second = self.make_phase3_heads_up_alert(**kwargs)
            scanner.evaluate_phase3_heads_up(first, self.make_phase3_heads_up_snapshot(), "Fresh", context)
            scanner.evaluate_phase3_heads_up(second, self.make_phase3_heads_up_snapshot(), "Fresh", context)
            self.assertTrue(first.phase3_heads_up_sent)
            self.assertFalse(second.phase3_heads_up_sent)
            self.assertTrue(second.phase3_heads_up_dedupe_blocked)

        def test_stock_only_phase3_duplicate_is_deduped(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            kwargs = {
                "scenario_stage": "LATE",
                "entry_quality_label": "LATE",
                "scenario_score": 83,
                "confirmation_score": 60,
                "scenario_top": {"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "LATE", "score": 83},
            }
            first = self.make_phase3_heads_up_alert(**kwargs)
            second = self.make_phase3_heads_up_alert(**kwargs)
            scanner.evaluate_phase3_heads_up(first, self.make_phase3_heads_up_snapshot(), "Fresh")
            scanner.evaluate_phase3_heads_up(second, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(first.phase3_heads_up_sent)
            self.assertFalse(second.phase3_heads_up_sent)
            self.assertTrue(second.phase3_heads_up_dedupe_blocked)

        def test_stock_only_phase3_conflict_blocks_except_rejection(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            conflicted = self.make_phase3_heads_up_alert(
                scenario_conflict=True,
                scenario_stage="LATE",
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "LATE", "score": 85},
            )
            scanner.evaluate_phase3_heads_up(conflicted, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertFalse(conflicted.phase3_heads_up_sent)
            rejection = self.make_phase3_heads_up_alert(
                direction="BEARISH",
                strategy_direction="bearish",
                scenario_direction="bearish",
                scenario_conflict=True,
                scenario_stage="LATE",
                candle_label="SELLER_CONTROL",
                scenario_top={"scenario_name": "Pullback Rejecting", "direction": "bearish", "stage": "LATE", "score": 85},
            )
            scanner.evaluate_phase3_heads_up(rejection, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(rejection.phase3_heads_up_sent)
            self.assertTrue(rejection.stock_only_heads_up_allowed)

        def test_context_symbols_are_collected_but_not_alert_symbols(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            snapshots = scanner.build_snapshots()
            self.assertEqual(scanner.symbols, ["AAPL"])
            self.assertEqual(scanner.context_symbols, ["SPY", "QQQ"])
            self.assertEqual(set(snapshots), {"AAPL", "SPY", "QQQ"})
            context = scanner.build_market_context(snapshots)
            self.assertEqual(set(context), {"SPY", "QQQ"})

        def test_phase3_heads_up_stale_data_blocks(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert()
            snap = self.make_phase3_heads_up_snapshot(stale=True)
            scanner.evaluate_phase3_heads_up(alert, snap, snapshot_data_quality(snap, scanner.config))
            self.assertFalse(alert.phase3_heads_up_sent)
            self.assertIn("stale", alert.phase3_heads_up_block_reason.lower())

        def test_phase3_heads_up_duplicate_within_dedupe_window_blocks(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            first = self.make_phase3_heads_up_alert()
            snap = self.make_phase3_heads_up_snapshot()
            scanner.evaluate_phase3_heads_up(first, snap, "Fresh")
            self.assertTrue(first.phase3_heads_up_sent)
            scanner.state_store.set_last_alert_time(scanner.phase3_heads_up_state_key(first), now_utc())
            second = self.make_phase3_heads_up_alert()
            scanner.evaluate_phase3_heads_up(second, snap, "Fresh")
            self.assertTrue(second.phase3_heads_up_eligible)
            self.assertFalse(second.phase3_heads_up_sent)
            self.assertTrue(second.phase3_heads_up_dedupe_blocked)
            self.assertIsNotNone(second.phase3_heads_up_next_eligible_time)
            self.assertIn("duplicate", second.phase3_heads_up_block_reason.lower())
            self.assertIsNotNone(second.phase3_heads_up_message_fingerprint)
            self.assertIsNotNone(second.phase3_heads_up_dedupe_reason)

        def test_phase3_heads_up_same_setup_after_cooldown_is_allowed(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            first = self.make_phase3_heads_up_alert()
            scanner.evaluate_phase3_heads_up(first, self.make_phase3_heads_up_snapshot(), "Fresh")
            old = now_utc() - timedelta(minutes=16)
            scanner.state_store.set_phase3_heads_up_record(first, old)
            scanner.state_store.set_last_alert_time(scanner.phase3_heads_up_state_key(first), old)
            scanner.state_store.save()
            second = self.make_phase3_heads_up_alert()
            scanner.evaluate_phase3_heads_up(second, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(second.phase3_heads_up_sent)
            self.assertIn("cooldown elapsed", second.phase3_heads_up_dedupe_reason or "")

        def test_phase3_heads_up_stage_change_is_allowed_during_cooldown(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            first = self.make_phase3_heads_up_alert(
                scenario_stage="FORMING",
                entry_quality_label="EARLY",
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "FORMING", "score": 84},
                scenario_score=84,
            )
            scanner.evaluate_phase3_heads_up(first, self.make_phase3_heads_up_snapshot(), "Fresh")
            second = self.make_phase3_heads_up_alert(
                scenario_stage="GOOD_POSITION",
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "GOOD_POSITION", "score": 87},
            )
            scanner.evaluate_phase3_heads_up(second, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(second.phase3_heads_up_sent)
            self.assertEqual(second.phase3_heads_up_dedupe_reason, "scenario stage changed")

        def test_phase3_heads_up_scenario_change_is_allowed_during_cooldown(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            first = self.make_phase3_heads_up_alert()
            scanner.evaluate_phase3_heads_up(first, self.make_phase3_heads_up_snapshot(), "Fresh")
            second = self.make_phase3_heads_up_alert(
                scenario_top={"scenario_name": "Bullish Trend Continuation", "direction": "bullish", "stage": "CONFIRMED", "score": 87},
            )
            scanner.evaluate_phase3_heads_up(second, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(second.phase3_heads_up_sent)
            self.assertEqual(second.phase3_heads_up_dedupe_reason, "scenario changed")

        def test_phase3_heads_up_score_change_ten_points_is_allowed_during_cooldown(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            first = self.make_phase3_heads_up_alert(
                scenario_score=80,
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "CONFIRMED", "score": 80},
            )
            scanner.evaluate_phase3_heads_up(first, self.make_phase3_heads_up_snapshot(), "Fresh")
            second = self.make_phase3_heads_up_alert(
                scenario_score=90,
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "CONFIRMED", "score": 90},
            )
            scanner.evaluate_phase3_heads_up(second, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(second.phase3_heads_up_sent)
            self.assertIn("10 points", second.phase3_heads_up_dedupe_reason or "")

        def test_phase3_heads_up_premarket_duplicate_is_blocked(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            premarket = set_today_time_et("08:15").astimezone(UTC)
            first = self.make_phase3_heads_up_alert(timestamp=premarket)
            second = self.make_phase3_heads_up_alert(timestamp=premarket + timedelta(seconds=20))
            scanner.evaluate_phase3_heads_up(first, self.make_phase3_heads_up_snapshot(), "Fresh")
            scanner.evaluate_phase3_heads_up(second, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(first.phase3_heads_up_sent)
            self.assertFalse(second.phase3_heads_up_sent)
            self.assertTrue(second.phase3_heads_up_dedupe_blocked)

        def test_phase3_heads_up_dedupe_key_includes_stage(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            confirmed = self.make_phase3_heads_up_alert()
            forming = self.make_phase3_heads_up_alert(
                scenario_stage="FORMING",
                scenario_top={"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "FORMING", "score": 87},
            )
            self.assertNotEqual(scanner.phase3_heads_up_state_key(confirmed), scanner.phase3_heads_up_state_key(forming))

        def test_phase3_heads_up_same_scan_duplicate_is_blocked(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            first = self.make_phase3_heads_up_alert(alert_score=70)
            second = self.make_phase3_heads_up_alert(alert_score=50)
            first.phase3_heads_up_sent = True
            second.phase3_heads_up_sent = True
            scanner.keep_best_text_alert_per_direction([first, second])
            self.assertTrue(first.phase3_heads_up_sent)
            self.assertFalse(second.phase3_heads_up_sent)
            self.assertTrue(second.phase3_heads_up_dedupe_blocked)

        def test_phase3_heads_up_missing_market_context_warns(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert()
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh", {})
            self.assertEqual(alert.market_confirmation_status, "UNAVAILABLE")
            self.assertIn("Market confirmation unavailable — check SPY/QQQ manually.", alert.scenario_warnings)

        def test_phase3_heads_up_indicative_options_do_not_block(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(option_feed_status="INDICATIVE")
            snap = self.make_phase3_heads_up_snapshot()
            scanner.evaluate_phase3_heads_up(alert, snap, "Fresh")
            self.assertTrue(alert.phase3_heads_up_sent)

        def test_phase7_poor_option_does_not_block_stock_only_heads_up(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                option_quality="WIDE_SPREAD",
                option_tradable=False,
                option_feed_status="OPRA",
            )
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertTrue(alert.phase3_heads_up_sent)
            self.assertFalse(alert.sms_allowed)
            self.assertTrue(alert.option_stock_only_allowed)

        def test_phase3_heads_up_does_not_change_normal_sms_rules(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert()
            snap = self.make_phase3_heads_up_snapshot()
            graded = scanner.grade_alert(alert, snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertTrue(graded.phase3_heads_up_sent)
            self.assertFalse(graded.sms_allowed)
            self.assertNotEqual(graded.alert_grade, "A+")

        def test_telegram_disabled_sends_nothing(self) -> None:
            from unittest.mock import patch
            notifier = TelegramNotifier("secret-token", "123", enabled=False, alert_types=["NORMAL_SMS"])
            alert = self.make_phase3_heads_up_alert()
            alert.sms_allowed = True
            alert.sms_sent = True
            alert.option_tradable = True
            alert.option_quality = "Tradable"
            with patch("requests.post") as post:
                notifier.send(alert)
            post.assert_not_called()

        def test_telegram_missing_credentials_does_not_crash(self) -> None:
            notifier = TelegramNotifier("", "", enabled=True, alert_types=["NORMAL_SMS"])
            alert = self.make_phase3_heads_up_alert()
            alert.sms_allowed = True
            notifier.send(alert)

        def test_telegram_test_alert_still_sends_to_group_chat(self) -> None:
            from unittest.mock import Mock, patch
            global NOTIFICATION_STATUS_LOG
            original_log = NOTIFICATION_STATUS_LOG
            temp_dir = Path(tempfile.mkdtemp())
            NOTIFICATION_STATUS_LOG = temp_dir / "notification_status.jsonl"
            response = Mock()
            response.raise_for_status.return_value = None
            previous = self.with_env({"TELEGRAM_BOT_TOKEN": "secret-token", "TELEGRAM_CHAT_ID": "-5213422925"})
            try:
                with patch("requests.post", return_value=response) as post:
                    self.assertTrue(send_telegram_test_message(load_config(None)))
                self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "-5213422925")
                logged = NOTIFICATION_STATUS_LOG.read_text()
                self.assertIn('"alert_type": "TEST"', logged)
                self.assertIn('"telegram_destination_type": "group"', logged)
                self.assertIn('"telegram_chat_id_last4": "2925"', logged)
                self.assertNotIn("-5213422925", logged)
            finally:
                self.restore_env(previous)
                NOTIFICATION_STATUS_LOG = original_log

        def test_preview_alert_tool_renders_all_cases_without_market_data_or_secrets(self) -> None:
            from tools import preview_alert_text

            rendered = preview_alert_text.render_cases("all")
            self.assertEqual(set(rendered), set(preview_alert_text.CASE_NAMES))
            for name, message in rendered.items():
                valid, reason = preview_alert_text.validate_message(name, message)
                self.assertTrue(valid, reason)
                self.assertIn("Invalidation:", message)
                self.assertIn("Option:", message)
                self.assertLessEqual(len(message), 900)

        def test_preview_alert_tool_only_sends_with_explicit_flag(self) -> None:
            import sys
            from unittest.mock import patch
            from tools import preview_alert_text

            with patch.object(sys, "argv", ["preview_alert_text.py", "--case", "mixed"]), patch.object(
                preview_alert_text.scanner, "send_telegram_message"
            ) as send:
                self.assertEqual(preview_alert_text.main(), 0)
                send.assert_not_called()
            with patch.object(
                sys,
                "argv",
                ["preview_alert_text.py", "--case", "mixed", "--send-telegram"],
            ), patch.object(
                preview_alert_text.scanner,
                "send_telegram_message",
                return_value=(True, ""),
            ) as send:
                self.assertEqual(preview_alert_text.main(), 0)
                send.assert_called_once()

        def test_preview_openai_missing_key_falls_back_without_request(self) -> None:
            from unittest.mock import Mock
            from tools import preview_alert_text

            alert = preview_alert_text.sample_alerts()["mixed"]
            rule = preview_alert_text.render_cases("mixed")["mixed"]
            request = Mock()
            result = preview_alert_text.format_with_openai("mixed", alert, rule, api_key="", request_fn=request)
            self.assertFalse(result["success"])
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["message"], rule)
            request.assert_not_called()

        def test_preview_openai_rejects_forbidden_language_and_fact_changes(self) -> None:
            from tools import preview_alert_text

            alert = preview_alert_text.sample_alerts()["mixed"]
            rule = preview_alert_text.render_cases("mixed")["mixed"]
            facts = preview_alert_text.extract_rule_facts("mixed", alert, rule)
            valid_output = {
                "title": facts["title"],
                "bias": "BEARISH structure with conflicting signals",
                "why": facts["why"],
                "market": facts["market"],
                "structure": facts["structure"],
                "risk": facts["risk"],
                "wait_for": facts["wait_for"],
                "invalidation": facts["invalidation"],
                "option": facts["option"],
                "reminder": preview_alert_text.DISCLAIMER,
                "final_message": "\n\n".join(
                    [
                        facts["title"],
                        f"Why:\n{facts['why']}",
                        f"Market:\n{facts['market']}",
                        f"Structure:\n{facts['structure']}",
                        f"Risk:\n{facts['risk']}",
                        f"Wait for:\n{facts['wait_for']}",
                        f"Invalidation:\n{facts['invalidation']}",
                        f"Option:\n{facts['option']}",
                        preview_alert_text.DISCLAIMER,
                    ]
                ),
            }
            forbidden = dict(valid_output, final_message=rule.replace("Why:", "Why: Buy now."))
            changed = dict(
                valid_output,
                title="AAPL TRADE QUALITY WATCH — Bullish Pullback Holding",
                bias="BULLISH",
                final_message=rule.replace("AAPL MIXED / NO TRADE", "AAPL TRADE QUALITY WATCH"),
            )
            self.assertFalse(preview_alert_text.validate_openai_output("mixed", forbidden, facts)[0])
            self.assertFalse(preview_alert_text.validate_openai_output("mixed", changed, facts)[0])

        def test_preview_openai_rejects_paragraph_and_missing_sections(self) -> None:
            from tools import preview_alert_text

            alert = preview_alert_text.sample_alerts()["mixed"]
            rule = preview_alert_text.render_cases("mixed")["mixed"]
            facts = preview_alert_text.extract_rule_facts("mixed", alert, rule)
            output = {
                "title": facts["title"],
                "bias": "BEARISH",
                "why": facts["why"],
                "market": facts["market"],
                "structure": facts["structure"],
                "risk": facts["risk"],
                "wait_for": facts["wait_for"],
                "invalidation": facts["invalidation"],
                "option": facts["option"],
                "reminder": preview_alert_text.DISCLAIMER,
                "final_message": (
                    f"{facts['title']}. {facts['why']} {facts['risk']} "
                    f"Invalidation: {facts['invalidation']}. Option: {facts['option']}. "
                    f"{preview_alert_text.DISCLAIMER}"
                ),
            }
            valid, reason = preview_alert_text.validate_openai_output("mixed", output, facts)
            self.assertFalse(valid)
            self.assertIn("labeled sections", reason)

        def test_preview_openai_rejects_missing_locked_sections_and_disclaimer(self) -> None:
            from tools import preview_alert_text

            alert = preview_alert_text.sample_alerts()["do_not_chase"]
            rule = preview_alert_text.render_cases("do_not_chase")["do_not_chase"]
            facts = preview_alert_text.extract_rule_facts("do_not_chase", alert, rule)
            output = {
                "title": facts["title"],
                "bias": "BEARISH",
                "why": facts["why"],
                "market": facts["market"],
                "structure": facts["structure"],
                "risk": facts["risk"],
                "wait_for": facts["wait_for"],
                "invalidation": facts["invalidation"],
                "option": facts["option"],
                "reminder": preview_alert_text.DISCLAIMER,
                "final_message": preview_alert_text.assemble_openai_message(
                    {
                        "why": facts["why"],
                        "market": facts["market"],
                        "structure": facts["structure"],
                        "risk": facts["risk"],
                        "wait_for": facts["wait_for"],
                    },
                    facts,
                ),
            }
            for missing in ("Invalidation:\n", "Option:\n", preview_alert_text.DISCLAIMER):
                changed = dict(output, final_message=output["final_message"].replace(missing, ""))
                self.assertFalse(preview_alert_text.validate_openai_output("do_not_chase", changed, facts)[0])

        def test_live_telegram_openai_formatter_enabled_disabled_and_fallback(self) -> None:
            from unittest.mock import patch
            from tools import preview_alert_text

            alert = self.make_phase3_heads_up_alert()
            assign_professional_alert_tier(alert)
            rule = professional_telegram_message(alert, "PHASE3_HEADS_UP")
            edited = rule.replace("Why:", "Why:\nEdited wording.\n\nOriginal why:")
            enabled = TelegramNotifier("", "", openai_formatter_enabled=True)
            disabled = TelegramNotifier("", "", openai_formatter_enabled=False)
            with patch.object(
                preview_alert_text,
                "format_with_openai",
                return_value={"message": edited, "success": True, "fallback_used": False, "error": "", "model": "test"},
            ) as formatter:
                self.assertEqual(enabled.format_message(alert, "PHASE3_HEADS_UP"), edited)
                formatter.assert_called_once()
            with patch.object(preview_alert_text, "format_with_openai") as formatter:
                self.assertEqual(disabled.format_message(alert, "PHASE3_HEADS_UP"), rule)
                formatter.assert_not_called()
            with patch.object(
                preview_alert_text,
                "format_with_openai",
                return_value={"message": rule, "success": False, "fallback_used": True, "error": "rejected", "model": "test"},
            ):
                self.assertEqual(enabled.format_message(alert, "PHASE3_HEADS_UP"), rule)

        def test_openai_alert_formatter_env_config(self) -> None:
            previous = self.with_env(
                {
                    "ENABLE_OPENAI_ALERT_FORMATTER": "false",
                    "OPENAI_ALERT_FORMATTER_STYLE": "section",
                    "OPENAI_ALERT_FORMATTER_FALLBACK": "true",
                    "OPENAI_ALERT_FORMATTER_MAX_CHARS": "850",
                }
            )
            try:
                notifications = load_config(None)["notifications"]
                self.assertFalse(notifications["openai_alert_formatter_enabled"])
                self.assertEqual(notifications["openai_alert_formatter_style"], "section")
                self.assertTrue(notifications["openai_alert_formatter_fallback"])
                self.assertEqual(notifications["openai_alert_formatter_max_chars"], 850)
            finally:
                self.restore_env(previous)

        def test_preview_openai_failure_redacts_key_and_logs_fallback(self) -> None:
            from tools import preview_alert_text

            original_log = preview_alert_text.OPENAI_FORMATTER_LOG
            temp_dir = Path(tempfile.mkdtemp())
            preview_alert_text.OPENAI_FORMATTER_LOG = temp_dir / "openai_alert_formatter.jsonl"
            secret = "openai-preview-secret"
            alert = preview_alert_text.sample_alerts()["mixed"]
            rule = preview_alert_text.render_cases("mixed")["mixed"]

            def fail_request(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError(f"request failed with {secret}")

            try:
                result = preview_alert_text.format_with_openai(
                    "mixed", alert, rule, api_key=secret, request_fn=fail_request
                )
                self.assertTrue(result["fallback_used"])
                self.assertNotIn(secret, result["error"])
                logged = preview_alert_text.OPENAI_FORMATTER_LOG.read_text(encoding="utf-8")
                self.assertIn('"fallback_used": true', logged)
                self.assertIn('"fallback_reason":', logged)
                self.assertIn('"validation_passed": false', logged)
                self.assertIn('"setup": "Mixed Signal"', logged)
                self.assertNotIn(secret, logged)
            finally:
                preview_alert_text.OPENAI_FORMATTER_LOG = original_log

        def test_preview_compare_openai_prints_both_formats(self) -> None:
            import io
            import sys
            from unittest.mock import patch
            from tools import preview_alert_text

            rule = preview_alert_text.render_cases("mixed")["mixed"]
            with patch.object(sys, "argv", ["preview_alert_text.py", "--case", "mixed", "--compare-openai"]), patch.object(
                preview_alert_text, "format_with_openai",
                return_value={"message": rule, "success": False, "fallback_used": True, "error": "test fallback", "model": "test"},
            ), patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(preview_alert_text.main(), 0)
                text = output.getvalue()
                self.assertIn("RULE-BASED FORMAT", text)
                self.assertIn("OPENAI FORMAT", text)

        def test_telegram_success_and_failure_are_logged_without_token(self) -> None:
            from unittest.mock import Mock, patch
            global NOTIFICATION_STATUS_LOG
            original_log = NOTIFICATION_STATUS_LOG
            temp_dir = Path(tempfile.mkdtemp())
            NOTIFICATION_STATUS_LOG = temp_dir / "notification_status.jsonl"
            token = "secret-telegram-token"
            notifier = TelegramNotifier(
                token,
                "-5213422925",
                enabled=True,
                alert_types=["NORMAL_SMS"],
                delivery_dedupe_state_path=temp_dir / "delivery_dedupe.json",
            )
            alert = self.make_phase3_heads_up_alert()
            alert.sms_allowed = True
            alert.sms_sent = True
            alert.option_tradable = True
            alert.option_quality = "Tradable"
            response = Mock()
            response.raise_for_status.return_value = None
            try:
                with patch("requests.post", return_value=response):
                    notifier.send(alert)
                alert.category = "SECOND APPROVED ALERT"
                with patch("requests.post", side_effect=RuntimeError(f"failed {token}")):
                    notifier.send(alert)
                text = NOTIFICATION_STATUS_LOG.read_text(encoding="utf-8")
                self.assertIn('"sent": true', text)
                self.assertIn('"sent": false', text)
                self.assertIn('"telegram_sent": true', text)
                self.assertIn('"telegram_chat_id": "[REDACTED]"', text)
                self.assertIn('"sms_sent": true', text)
                self.assertIn('"alert_tier": "TRADE_QUALITY_WATCH"', text)
                self.assertIn('"alert_tier_reason":', text)
                self.assertIn('"message_source_path": "format_alert_message"', text)
                self.assertIn("[REDACTED]", text)
                self.assertNotIn(token, text)
            finally:
                NOTIFICATION_STATUS_LOG = original_log

        def test_scanner_identity_appears_in_startup_status_log(self) -> None:
            global SCANNER_STARTUP_STATUS_LOG
            original_log = SCANNER_STARTUP_STATUS_LOG
            temp_dir = Path(tempfile.mkdtemp())
            SCANNER_STARTUP_STATUS_LOG = temp_dir / "scanner_startup_status.jsonl"
            previous = self.with_env(
                {
                    "SCANNER_INSTANCE_NAME": "TestMac",
                    "SCANNER_MACHINE_ROLE": "backup",
                    "SCANNER_ALERT_PROFILE": "AAPL_TESTING",
                    "TELEGRAM_CHAT_ID": "-5213422925",
                    "TELEGRAM_BOT_TOKEN": "startup-secret-token",
                    "ALPACA_API_KEY": "startup-secret-key",
                    "ALPACA_SECRET_KEY": "startup-secret-value",
                }
            )
            try:
                payload = log_scanner_startup_status(load_config(None), {"stock_feed_status": "SIP", "options_feed_status": "OPRA"})
                logged = SCANNER_STARTUP_STATUS_LOG.read_text()
                self.assertEqual(payload["scanner_instance_name"], "TestMac")
                self.assertEqual(payload["scanner_alert_profile"], "AAPL_TESTING")
                self.assertEqual(payload["alert_symbols"], ["AAPL"])
                self.assertEqual(payload["context_symbols"], ["SPY", "QQQ"])
                self.assertIn('"telegram_chat_id_last4": "2925"', logged)
                self.assertNotIn("-5213422925", logged)
                self.assertNotIn("startup-secret-token", logged)
                self.assertNotIn("startup-secret-key", logged)
                self.assertNotIn("startup-secret-value", logged)
            finally:
                self.restore_env(previous)
                SCANNER_STARTUP_STATUS_LOG = original_log

        def test_official_aapl_testing_profile_loads_with_context_only_symbols(self) -> None:
            previous = self.with_env(
                {
                    "SCANNER_ALERT_PROFILE": "AAPL_TESTING",
                    "ALERT_SYMBOLS": "AAPL",
                    "MARKET_CONTEXT_SYMBOLS": "SPY,QQQ",
                }
            )
            try:
                config = load_config(None)
                identity = scanner_identity(config)
                self.assertEqual(identity["scanner_alert_profile"], "AAPL_TESTING")
                self.assertEqual(identity["alert_symbols"], ["AAPL"])
                self.assertEqual(identity["context_symbols"], ["SPY", "QQQ"])
                self.assertNotIn("SPY", config["symbols"])
                self.assertNotIn("QQQ", config["symbols"])
            finally:
                self.restore_env(previous)

        def test_telegram_only_sends_approved_phase3_or_normal_sms(self) -> None:
            from unittest.mock import patch
            notifier = TelegramNotifier(
                "secret-token",
                "123",
                enabled=True,
                alert_types=["PHASE3_HEADS_UP", "NORMAL_SMS"],
            )
            blocked = self.make_phase3_heads_up_alert(risk_label="HIGH", entry_quality_label="LATE")
            blocked.phase3_heads_up_sent = False
            blocked.sms_allowed = False
            with patch("requests.post") as post:
                notifier.send(blocked)
            post.assert_not_called()

        def test_telegram_phase3_uses_existing_dedupe_decision(self) -> None:
            from unittest.mock import patch
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert()
            alert.phase3_heads_up_type = "GOOD_POSITION"
            scanner.state_store.set_last_alert_time(scanner.phase3_heads_up_state_key(alert), now_utc())
            scanner.evaluate_phase3_heads_up(alert, self.make_phase3_heads_up_snapshot(), "Fresh")
            self.assertFalse(alert.phase3_heads_up_sent)
            notifier = TelegramNotifier("secret-token", "123", enabled=True, alert_types=["PHASE3_HEADS_UP"])
            with patch("requests.post") as post:
                notifier.send(alert)
            post.assert_not_called()

        def test_telegram_phase3_identical_alert_three_times_only_sends_once(self) -> None:
            from unittest.mock import Mock, patch
            global NOTIFICATION_STATUS_LOG, LOG_DIR
            original_notification_log = NOTIFICATION_STATUS_LOG
            original_log_dir = LOG_DIR
            temp_dir = Path(tempfile.mkdtemp())
            NOTIFICATION_STATUS_LOG = temp_dir / "notification_status.jsonl"
            LOG_DIR = temp_dir
            notifier = TelegramNotifier(
                "secret-token",
                "123",
                enabled=True,
                alert_types=["PHASE3_HEADS_UP"],
                phase3_dedupe_state_path=temp_dir / "telegram_dedupe.json",
                delivery_dedupe_state_path=temp_dir / "delivery_dedupe.json",
            )
            alerts = [self.make_phase3_heads_up_alert() for _ in range(3)]
            for alert in alerts:
                alert.phase3_heads_up_sent = True
                alert.phase3_heads_up_type = "GOOD_POSITION"
            response = Mock()
            response.raise_for_status.return_value = None
            try:
                with patch("requests.post", return_value=response) as post:
                    for alert in alerts:
                        notifier.send(alert)
                self.assertEqual(post.call_count, 1)
                self.assertFalse(alerts[1].phase3_heads_up_sent)
                self.assertFalse(alerts[2].phase3_heads_up_sent)
                self.assertTrue(alerts[1].phase3_heads_up_dedupe_blocked)
            finally:
                NOTIFICATION_STATUS_LOG = original_notification_log
                LOG_DIR = original_log_dir

        def test_telegram_normal_watch_alert_is_mirrored(self) -> None:
            from unittest.mock import Mock, patch
            global NOTIFICATION_STATUS_LOG
            original_log = NOTIFICATION_STATUS_LOG
            temp_dir = Path(tempfile.mkdtemp())
            NOTIFICATION_STATUS_LOG = temp_dir / "notification_status.jsonl"
            notifier = TelegramNotifier(
                "secret-token",
                "-5213422925",
                enabled=True,
                alert_types=["PHASE3_HEADS_UP", "NORMAL_SMS"],
                delivery_dedupe_state_path=temp_dir / "delivery_dedupe.json",
            )
            alert = self.make_phase3_heads_up_alert(category="WATCH AAPL BULLISH")
            alert.phase3_heads_up_sent = False
            alert.sms_allowed = False
            alert.watch_allowed = True
            alert.sms_sent = True
            response = Mock()
            response.raise_for_status.return_value = None
            try:
                with patch("requests.post", return_value=response) as post:
                    notifier.send(alert)
                post.assert_called_once()
                payload = post.call_args.kwargs["json"]
                self.assertEqual(payload["chat_id"], "-5213422925")
                record = json.loads(NOTIFICATION_STATUS_LOG.read_text().splitlines()[-1])
                self.assertEqual(record["alert_type"], "NORMAL_WATCH")
                self.assertTrue(record["telegram_sent"])
                self.assertTrue(record["sms_sent"])
            finally:
                NOTIFICATION_STATUS_LOG = original_log

        def test_telegram_sms_approved_alert_is_mirrored(self) -> None:
            from unittest.mock import Mock, patch
            global NOTIFICATION_STATUS_LOG
            original_log = NOTIFICATION_STATUS_LOG
            temp_dir = Path(tempfile.mkdtemp())
            NOTIFICATION_STATUS_LOG = temp_dir / "notification_status.jsonl"
            notifier = TelegramNotifier(
                "secret-token",
                "-5213422925",
                enabled=True,
                alert_types=["NORMAL_SMS"],
                delivery_dedupe_state_path=temp_dir / "delivery_dedupe.json",
            )
            alert = self.make_phase3_heads_up_alert()
            alert.phase3_heads_up_sent = False
            alert.sms_allowed = True
            response = Mock()
            response.raise_for_status.return_value = None
            try:
                with patch("requests.post", return_value=response) as post:
                    notifier.send(alert)
                post.assert_called_once()
            finally:
                NOTIFICATION_STATUS_LOG = original_log

        def test_approved_messages_watch_is_mirrored_to_telegram(self) -> None:
            from unittest.mock import Mock, patch
            global NOTIFICATION_STATUS_LOG
            original_log = NOTIFICATION_STATUS_LOG
            temp_dir = Path(tempfile.mkdtemp())
            NOTIFICATION_STATUS_LOG = temp_dir / "notification_status.jsonl"
            telegram = TelegramNotifier(
                "secret-token",
                "-5213422925",
                enabled=True,
                alert_types=["NORMAL_SMS"],
                delivery_dedupe_state_path=temp_dir / "delivery_dedupe.json",
            )
            notifier = CompositeNotifier([MessagesNotifier("5551234567", send_watch=True), telegram])
            alert = self.make_phase3_heads_up_alert(category="WATCH AAPL BULLISH")
            alert.phase3_heads_up_sent = False
            alert.sms_allowed = False
            alert.watch_allowed = True
            response = Mock()
            response.raise_for_status.return_value = None
            try:
                with patch("subprocess.run") as messages_send, patch("requests.post", return_value=response) as telegram_send:
                    notifier.send(alert)
                messages_send.assert_called_once()
                telegram_send.assert_called_once()
                self.assertTrue(alert.sms_sent)
            finally:
                NOTIFICATION_STATUS_LOG = original_log

        def test_telegram_normal_alert_duplicate_is_blocked(self) -> None:
            from unittest.mock import Mock, patch
            global NOTIFICATION_STATUS_LOG
            original_log = NOTIFICATION_STATUS_LOG
            temp_dir = Path(tempfile.mkdtemp())
            NOTIFICATION_STATUS_LOG = temp_dir / "notification_status.jsonl"
            notifier = TelegramNotifier(
                "secret-token",
                "-5213422925",
                enabled=True,
                alert_types=["NORMAL_SMS"],
                delivery_dedupe_state_path=temp_dir / "delivery_dedupe.json",
            )
            alert = self.make_phase3_heads_up_alert()
            alert.phase3_heads_up_sent = False
            alert.sms_allowed = True
            response = Mock()
            response.raise_for_status.return_value = None
            try:
                with patch("requests.post", return_value=response) as post:
                    notifier.send(alert)
                    notifier.send(alert)
                self.assertEqual(post.call_count, 1)
            finally:
                NOTIFICATION_STATUS_LOG = original_log

        def test_telegram_env_config(self) -> None:
            previous = self.with_env(
                {
                    "ENABLE_TELEGRAM_ALERTS": "true",
                    "TELEGRAM_ALERT_TYPES": "PHASE3_HEADS_UP,NORMAL_SMS",
                    "TELEGRAM_AAPL_ONLY": "true",
                    "TELEGRAM_SEND_TEST_ON_START": "false",
                    "TELEGRAM_TIMEOUT_SECONDS": "8",
                }
            )
            try:
                notifications = load_config(None)["notifications"]
                self.assertTrue(notifications["telegram_enabled"])
                self.assertEqual(notifications["telegram_alert_types"], ["PHASE3_HEADS_UP", "NORMAL_SMS"])
                self.assertTrue(notifications["telegram_aapl_only"])
                self.assertEqual(notifications["telegram_timeout_seconds"], 8)
            finally:
                self.restore_env(previous)

        def test_strategy_below_threshold_does_not_send_sms(self) -> None:
            config = load_config(None)
            bars = self.make_strategy_bars([100.0 + i * 0.1 for i in range(12)])
            snap = SymbolSnapshot(symbol="TEST", latest_bar=bars[-1], recent_bars=bars, opening_range_high=100.5, opening_range_low=99.5)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_strategy_threshold.csv", LOG_DIR / "test_strategy_threshold.jsonl"),
                StateStore(STATE_DIR / "test_strategy_threshold_state.json"),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=101.1,
                fast_move_pct=1.0,
                day_move_pct=1.0,
                relative_volume=2.0,
                opening_range_high=100.5,
                option_quality="Tradable",
                options_score=90,
                primary_setup="Weak Breakout",
                strategy_confidence_score=45,
                strategy_confidence_label="LOW",
            )
            graded = scanner.grade_alert(alert, snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertFalse(graded.sms_allowed)

        def test_phase2a_strong_breakout_volume_increases_confidence(self) -> None:
            bars = self.make_strategy_bars([99.2, 99.5, 99.8, 100.1, 100.4, 101.2], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            strong = self.strategy_summary(bars, {"pmh": 101.0}, rel_vol=2.5)
            weak = self.strategy_summary(bars, {"pmh": 101.0}, rel_vol=0.8)
            self.assertGreater(strong["confidence_score"], weak["confidence_score"])
            self.assertEqual(strong["volume_label"], "STRONG")

        def test_phase2a_weak_breakout_volume_lowers_confidence(self) -> None:
            bars = self.make_strategy_bars([99.2, 99.5, 99.8, 100.1, 100.4, 101.2], volumes=[2000, 2000, 2000, 2000, 2000, 500])
            summary = self.strategy_summary(bars, {"pmh": 101.0}, rel_vol=0.6)
            self.assertEqual(summary["volume_label"], "WEAK")
            self.assertTrue(any("low volume" in warning.lower() or "weak" in warning.lower() for warning in summary["warnings"]))

        def test_phase2a_sweep_reclaim_strong_volume_increases_confidence(self) -> None:
            bars = self.make_strategy_bars(
                [100.2, 99.8, 99.4, 98.9, 99.2, 99.7],
                lows=[100.0, 99.6, 99.2, 98.5, 98.8, 99.1],
                volumes=[1000, 1000, 1000, 2200, 2500, 3600],
            )
            summary = self.strategy_summary(bars, {"pml": 99.0}, rel_vol=2.2)
            self.assertEqual(summary["volume_label"], "STRONG")
            self.assertTrue(any("Sweep/reclaim" in reason for reason in summary["volume_quality"]["reasons"]))

        def test_phase2a_low_volume_reclaim_produces_warning(self) -> None:
            bars = self.make_strategy_bars(
                [100.2, 99.8, 99.4, 98.9, 99.2, 99.7],
                lows=[100.0, 99.6, 99.2, 98.5, 98.8, 99.1],
                volumes=[2000, 2000, 2000, 2000, 2000, 600],
            )
            summary = self.strategy_summary(bars, {"pml": 99.0}, rel_vol=0.5)
            self.assertEqual(summary["volume_label"], "WEAK")
            self.assertTrue(summary["warnings"])

        def test_phase2a_climax_after_multiple_large_candles_warns_exhaustion(self) -> None:
            bars = self.make_strategy_bars(
                [99.0, 99.4, 99.9, 100.6, 101.5, 102.6],
                highs=[99.2, 99.6, 100.1, 100.8, 101.7, 102.8],
                lows=[98.9, 99.2, 99.7, 100.2, 101.1, 102.0],
                volumes=[1000, 1000, 1300, 2800, 3600, 6000],
            )
            summary = self.strategy_summary(bars, {"pmh": 100.0}, rel_vol=4.0)
            self.assertEqual(summary["volume_label"], "CLIMAX")
            self.assertTrue(any("exhaustion" in warning.lower() for warning in summary["warnings"]))

        def test_phase2a_pullback_volume_on_retest_hold_not_automatically_bearish(self) -> None:
            from strategies.confirmation.volume_quality import evaluate_volume_quality
            bars = self.make_strategy_bars(
                [99.5, 99.9, 100.4, 100.8, 100.3, 100.6],
                lows=[99.3, 99.7, 100.1, 100.5, 100.0, 100.2],
                volumes=[1000, 1200, 3000, 2800, 1600, 1000],
            )
            result = evaluate_volume_quality(
                bars,
                load_config(None),
                relative_volume=1.0,
                direction="bullish",
                setup_label="Breakout Retest Holding",
            )
            self.assertTrue(any("Pullback/retest volume is lighter" in reason or "Retest/pullback volume is controlled" in reason for reason in result["reasons"]))
            self.assertFalse(result["is_volume_exhausted"])

        def test_phase2b_bullish_candle_near_high_scores_buyer_control(self) -> None:
            from strategies.confirmation.candle_strength import evaluate_candle_strength
            bars = self.make_strategy_bars(
                [100.0, 100.0, 100.0, 100.0, 100.0, 101.0],
                highs=[100.2, 100.2, 100.2, 100.2, 100.2, 101.1],
                lows=[99.8, 99.8, 99.8, 99.8, 99.8, 100.0],
            )
            result = evaluate_candle_strength(bars, load_config(None), direction="bullish")
            self.assertEqual(result["candle_label"], "BUYER_CONTROL")
            self.assertGreaterEqual(result["close_position_pct"], 75)

        def test_phase2b_bearish_candle_near_low_scores_seller_control(self) -> None:
            from strategies.confirmation.candle_strength import evaluate_candle_strength
            bars = self.make_strategy_bars(
                [100.0, 100.0, 100.0, 100.0, 100.0, 99.0],
                highs=[100.2, 100.2, 100.2, 100.2, 100.2, 100.0],
                lows=[99.8, 99.8, 99.8, 99.8, 99.8, 98.9],
            )
            result = evaluate_candle_strength(bars, load_config(None), direction="bearish")
            self.assertEqual(result["candle_label"], "SELLER_CONTROL")
            self.assertLessEqual(result["close_position_pct"], 25)

        def test_phase2b_breakout_large_upper_wick_gets_fakeout_warning(self) -> None:
            bars = self.make_strategy_bars(
                [99.8, 100.0, 100.1, 100.2, 100.0, 100.6],
                highs=[100.0, 100.2, 100.3, 100.4, 100.2, 102.0],
                lows=[99.6, 99.8, 99.9, 100.0, 99.8, 99.8],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            summary = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.0)
            self.assertEqual(summary["candle_label"], "REJECTION")
            self.assertTrue(any("upper wick rejection" in warning.lower() for warning in summary["warnings"]))

        def test_phase2b_breakdown_large_lower_wick_gets_reclaim_warning(self) -> None:
            bars = self.make_strategy_bars(
                [100.4, 100.2, 100.1, 99.9, 100.0, 99.4],
                highs=[100.6, 100.4, 100.3, 100.1, 100.2, 100.2],
                lows=[100.2, 100.0, 99.9, 99.7, 99.8, 98.0],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            summary = self.strategy_summary(bars, {"pml": 99.5}, rel_vol=2.0)
            self.assertEqual(summary["candle_label"], "REJECTION")
            self.assertTrue(any("lower wick" in warning.lower() for warning in summary["warnings"]))

        def test_phase2b_small_body_high_volume_gets_indecision_warning(self) -> None:
            from strategies.confirmation.candle_strength import evaluate_candle_strength
            bars = self.make_strategy_bars(
                [100.0, 100.0, 100.0, 100.0, 100.0, 100.05],
                highs=[100.2, 100.2, 100.2, 100.2, 100.2, 100.5],
                lows=[99.8, 99.8, 99.8, 99.8, 99.8, 99.5],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            result = evaluate_candle_strength(
                bars,
                load_config(None),
                direction="bullish",
                volume_quality={"volume_label": "STRONG"},
            )
            self.assertEqual(result["candle_label"], "INDECISION")
            self.assertTrue(any("churn" in warning.lower() for warning in result["warnings"]))

        def test_phase2b_candle_strength_integrates_with_strategy_scoring(self) -> None:
            buyer_bars = self.make_strategy_bars(
                [99.8, 100.0, 100.1, 100.2, 100.0, 101.0],
                highs=[100.0, 100.2, 100.3, 100.4, 100.2, 101.1],
                lows=[99.6, 99.8, 99.9, 100.0, 99.8, 100.0],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            rejection_bars = self.make_strategy_bars(
                [99.8, 100.0, 100.1, 100.2, 100.0, 100.6],
                highs=[100.0, 100.2, 100.3, 100.4, 100.2, 102.0],
                lows=[99.6, 99.8, 99.9, 100.0, 99.8, 99.8],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            buyer = self.strategy_summary(buyer_bars, {"pmh": 100.5}, rel_vol=2.0)
            rejection = self.strategy_summary(rejection_bars, {"pmh": 100.5}, rel_vol=2.0)
            self.assertEqual(buyer["candle_label"], "BUYER_CONTROL")
            self.assertGreater(buyer["confidence_score"], rejection["confidence_score"])
            self.assertGreater(buyer["confirmation_score"], rejection["confirmation_score"])

        def test_phase2c_breakout_above_pmh_then_retest_holds(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.8, 100.2, 100.6, 100.08, 100.12],
                highs=[99.7, 100.0, 100.4, 100.8, 100.25, 100.22],
                lows=[99.3, 99.6, 100.05, 100.3, 100.0, 100.02],
                volumes=[1000, 1000, 2500, 2800, 1500, 1600],
            )
            summary = self.strategy_summary(bars, {"pmh": 100.0}, rel_vol=1.6)
            self.assertTrue(summary["retest_hold"]["retest_active"])
            self.assertEqual(summary["retest_hold"]["retest_type"], "BREAKOUT_RETEST_HOLD")
            self.assertEqual(summary["entry_quality_label"], "GOOD_POSITION")
            self.assertIn("Breakout Retest Holding", summary["secondary_setups"])

        def test_phase2c_breakout_without_retest_has_no_retest_alert(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.8, 100.2, 100.6, 100.8, 101.0],
                highs=[99.7, 100.0, 100.4, 100.8, 101.0, 101.2],
                lows=[99.3, 99.6, 100.15, 100.35, 100.65, 100.85],
                volumes=[1000, 1000, 2500, 2800, 2200, 2300],
            )
            summary = self.strategy_summary(bars, {"pmh": 100.0}, rel_vol=1.7)
            self.assertFalse(summary["retest_hold"]["retest_active"])
            self.assertNotIn("Breakout Retest Holding", summary["secondary_setups"])

        def test_phase2c_price_breaks_and_loses_level_gets_fakeout_warning(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.8, 100.3, 100.5, 99.95, 99.9],
                highs=[99.7, 100.0, 100.5, 100.7, 100.2, 100.05],
                lows=[99.3, 99.6, 100.1, 100.2, 99.8, 99.75],
                volumes=[1000, 1000, 2500, 2800, 2600, 2400],
            )
            from strategies.confirmation.retest_hold import evaluate_retest_hold
            result = evaluate_retest_hold(bars, load_config(None), {"pmh": 100.0}, direction="bullish")
            self.assertFalse(result["retest_active"])
            self.assertTrue(any("fakeout" in warning.lower() for warning in result["warnings"]))

        def test_phase2c_breakdown_below_pml_then_underside_rejects(self) -> None:
            bars = self.make_strategy_bars(
                [100.5, 100.2, 99.8, 99.4, 99.92, 99.88],
                highs=[100.7, 100.4, 99.95, 99.6, 99.98, 99.97],
                lows=[100.3, 100.0, 99.6, 99.2, 99.7, 99.65],
                volumes=[1000, 1000, 2500, 2800, 1500, 1700],
            )
            summary = self.strategy_summary(bars, {"pml": 100.0}, rel_vol=1.7)
            self.assertTrue(summary["retest_hold"]["retest_active"])
            self.assertEqual(summary["retest_hold"]["retest_type"], "BREAKDOWN_RETEST_REJECT")
            self.assertIn("Breakdown Retest Rejecting", summary["secondary_setups"])

        def test_phase2c_retest_too_far_from_level_is_late(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.8, 100.2, 100.6, 100.8, 101.0],
                highs=[99.7, 100.0, 100.4, 100.8, 101.0, 101.2],
                lows=[99.3, 99.6, 100.0, 100.3, 100.6, 100.8],
                volumes=[1000, 1000, 2500, 2800, 2200, 2300],
            )
            from strategies.confirmation.retest_hold import evaluate_retest_hold
            result = evaluate_retest_hold(bars, load_config(None), {"pmh": 100.0}, direction="bullish")
            self.assertFalse(result["retest_active"])
            self.assertEqual(result["entry_quality_label"], "LATE")
            self.assertTrue(any("late entry" in warning.lower() for warning in result["warnings"]))

        def test_phase2c_high_selling_volume_on_pullback_lowers_score(self) -> None:
            controlled_bars = self.make_strategy_bars(
                [99.5, 99.8, 100.2, 100.6, 100.08, 100.12],
                highs=[99.7, 100.0, 100.4, 100.8, 100.25, 100.22],
                lows=[99.3, 99.6, 100.05, 100.3, 100.0, 100.02],
                volumes=[1000, 1000, 2500, 2800, 1500, 1600],
            )
            heavy_bars = self.make_strategy_bars(
                [99.5, 99.8, 100.2, 100.6, 100.08, 100.12],
                highs=[99.7, 100.0, 100.4, 100.8, 100.25, 100.22],
                lows=[99.3, 99.6, 100.05, 100.3, 100.0, 100.02],
                volumes=[1000, 1000, 2500, 2800, 4000, 4200],
            )
            from strategies.confirmation.retest_hold import evaluate_retest_hold
            controlled = evaluate_retest_hold(controlled_bars, load_config(None), {"pmh": 100.0}, direction="bullish")
            heavy = evaluate_retest_hold(heavy_bars, load_config(None), {"pmh": 100.0}, direction="bullish")
            self.assertTrue(heavy["retest_active"])
            self.assertLess(heavy["score"], controlled["score"])
            self.assertTrue(any("volume expanded" in warning.lower() for warning in heavy["warnings"]))

        def test_phase2d_clean_breakout_near_level_has_normal_extension(self) -> None:
            from strategies.confirmation.extension_exhaustion import evaluate_extension_exhaustion
            bars = self.make_strategy_bars(
                [99.8, 100.0, 100.1, 100.2, 100.3, 100.35],
                highs=[100.0, 100.2, 100.3, 100.4, 100.45, 100.45],
                lows=[99.6, 99.8, 99.9, 100.0, 100.15, 100.25],
            )
            result = evaluate_extension_exhaustion(bars, load_config(None), {"pmh": 100.2}, direction="bullish")
            self.assertEqual(result["extension_label"], "NORMAL")

        def test_phase2d_clean_breakout_far_above_vwap_gets_extended_warning(self) -> None:
            bars = self.make_strategy_bars(
                [99.8, 100.0, 100.1, 100.2, 100.4, 102.0],
                highs=[100.0, 100.2, 100.3, 100.4, 100.6, 102.2],
                lows=[99.6, 99.8, 99.9, 100.0, 100.2, 101.6],
                volumes=[1000, 1000, 1000, 1000, 1300, 3600],
            )
            summary = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.5)
            self.assertIn(summary["extension_label"], {"EXTENDED", "VERY_EXTENDED", "DO_NOT_CHASE"})
            self.assertTrue(any("extended from vwap" in warning.lower() for warning in summary["warnings"]))

        def test_phase2d_three_strong_candles_warn_late_entry(self) -> None:
            from strategies.confirmation.extension_exhaustion import evaluate_extension_exhaustion
            bars = self.make_strategy_bars(
                [100.0, 100.2, 100.9, 101.7, 102.5],
                highs=[100.2, 100.4, 101.0, 101.8, 102.6],
                lows=[99.8, 100.0, 100.2, 100.9, 101.7],
            )
            result = evaluate_extension_exhaustion(bars, load_config(None), {"pmh": 100.5}, direction="bullish")
            self.assertGreaterEqual(result["consecutive_large_candles"], 3)
            self.assertTrue(any("multiple large candles" in warning.lower() for warning in result["warnings"]))

        def test_phase2d_volume_climax_after_extension_gets_exhaustion_warning(self) -> None:
            from strategies.confirmation.extension_exhaustion import evaluate_extension_exhaustion
            bars = self.make_strategy_bars(
                [99.8, 100.0, 100.8, 101.7, 102.7],
                highs=[100.0, 100.2, 101.0, 101.9, 102.9],
                lows=[99.6, 99.8, 100.0, 100.8, 101.7],
            )
            result = evaluate_extension_exhaustion(
                bars,
                load_config(None),
                {"pmh": 100.2},
                direction="bullish",
                volume_quality={"volume_label": "CLIMAX", "is_volume_exhausted": True},
            )
            self.assertTrue(any("exhaustion" in warning.lower() for warning in result["warnings"]))

        def test_phase2d_do_not_chase_risk_overrides_normal_risk(self) -> None:
            bars = self.make_strategy_bars(
                [99.0, 99.4, 100.4, 101.6, 102.8, 104.2],
                highs=[99.2, 99.6, 100.6, 101.8, 103.0, 104.4],
                lows=[98.8, 99.2, 99.4, 100.4, 101.6, 102.8],
                volumes=[1000, 1000, 2400, 3000, 4200, 6000],
            )
            summary = self.strategy_summary(bars, {"pmh": 100.0}, rel_vol=4.0)
            self.assertEqual(summary["risk_label"], "DO_NOT_CHASE")
            self.assertEqual(summary["entry_quality_label"], "DO_NOT_CHASE")

        def test_phase2e_aapl_strong_while_qqq_flat_is_rs_strong(self) -> None:
            from strategies.confirmation.relative_strength import evaluate_relative_strength
            bars = self.make_strategy_bars([100.0, 100.1, 100.3, 100.6, 100.9, 101.2])
            qqq = self.make_strategy_bars([100.0, 100.0, 100.02, 100.01, 100.0, 100.03])
            spy = self.make_strategy_bars([100.0, 100.01, 100.0, 100.02, 100.01, 100.02])
            result = evaluate_relative_strength("AAPL", bars, load_config(None), {"SPY": spy, "QQQ": qqq}, direction="bullish")
            self.assertEqual(result["relative_strength_label"], "STRONG")
            self.assertGreater(result["symbol_vs_qqq"], 0.2)

        def test_phase2e_aapl_weak_while_qqq_strong_is_rw_weak(self) -> None:
            from strategies.confirmation.relative_strength import evaluate_relative_strength
            bars = self.make_strategy_bars([100.0, 99.9, 99.8, 99.7, 99.6, 99.5])
            qqq = self.make_strategy_bars([100.0, 100.2, 100.4, 100.6, 100.8, 101.0])
            spy = self.make_strategy_bars([100.0, 100.1, 100.2, 100.3, 100.4, 100.5])
            result = evaluate_relative_strength("AAPL", bars, load_config(None), {"SPY": spy, "QQQ": qqq}, direction="bullish")
            self.assertEqual(result["relative_strength_label"], "WEAK")
            self.assertTrue(any("underperforming" in warning.lower() for warning in result["warnings"]))

        def test_phase2e_bullish_setup_with_rs_gets_confidence_boost(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.7, 99.9, 100.1, 100.4, 101.0],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            qqq = self.make_strategy_bars([100.0, 100.05, 100.1, 100.15, 100.2, 100.25])
            spy = self.make_strategy_bars([100.0, 100.04, 100.08, 100.12, 100.16, 100.2])
            strong = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.0, market_bars={"SPY": spy, "QQQ": qqq})
            neutral = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.0, market_bars={})
            self.assertEqual(strong["relative_strength_label"], "STRONG")
            self.assertGreater(strong["confidence_score"], neutral["confidence_score"])

        def test_phase2e_bullish_setup_with_relative_weakness_gets_warning(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.7, 99.9, 100.1, 100.4, 100.8],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            qqq = self.make_strategy_bars([100.0, 100.5, 101.0, 101.5, 102.0, 102.5])
            spy = self.make_strategy_bars([100.0, 100.3, 100.6, 100.9, 101.2, 101.5])
            summary = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.0, market_bars={"SPY": spy, "QQQ": qqq})
            self.assertEqual(summary["relative_strength_label"], "WEAK")
            self.assertTrue(any("lacks relative strength" in warning.lower() for warning in summary["warnings"]))

        def test_phase2e_bearish_setup_with_rw_gets_boost(self) -> None:
            bars = self.make_strategy_bars(
                [101.0, 100.8, 100.4, 100.0, 99.6, 98.9],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            qqq = self.make_strategy_bars([100.0, 99.95, 99.9, 99.85, 99.8, 99.75])
            spy = self.make_strategy_bars([100.0, 99.96, 99.92, 99.88, 99.84, 99.8])
            weak = self.strategy_summary(bars, {"pml": 99.2}, rel_vol=2.0, market_bars={"SPY": spy, "QQQ": qqq})
            neutral = self.strategy_summary(bars, {"pml": 99.2}, rel_vol=2.0, market_bars={})
            self.assertEqual(weak["relative_strength_label"], "WEAK")
            self.assertGreater(weak["confidence_score"], neutral["confidence_score"])

        def test_phase2f_spy_qqq_above_vwap_higher_highs_bull_trend(self) -> None:
            from strategies.confirmation.market_regime import evaluate_market_regime
            spy = self.make_strategy_bars([100 + i * 0.12 for i in range(15)])
            qqq = self.make_strategy_bars([100 + i * 0.15 for i in range(15)])
            result = evaluate_market_regime({"SPY": spy, "QQQ": qqq}, load_config(None))
            self.assertEqual(result["market_regime"], "OPENING_DRIVE_UP")

        def test_phase2f_spy_qqq_below_vwap_lower_lows_bear_trend(self) -> None:
            from strategies.confirmation.market_regime import evaluate_market_regime
            spy = self.make_strategy_bars([102 - i * 0.12 for i in range(15)])
            qqq = self.make_strategy_bars([102 - i * 0.15 for i in range(15)])
            result = evaluate_market_regime({"SPY": spy, "QQQ": qqq}, load_config(None))
            self.assertEqual(result["market_regime"], "OPENING_DRIVE_DOWN")

        def test_phase2f_multiple_vwap_crosses_is_choppy(self) -> None:
            from strategies.confirmation.market_regime import evaluate_market_regime
            closes = [100.0, 100.4, 99.7, 100.3, 99.8, 100.2, 99.7, 100.3, 99.8, 100.2, 99.7, 100.3, 99.8, 100.2, 99.9]
            spy = self.make_strategy_bars(closes)
            qqq = self.make_strategy_bars(closes)
            result = evaluate_market_regime({"SPY": spy, "QQQ": qqq}, load_config(None))
            self.assertEqual(result["market_regime"], "CHOPPY")

        def test_phase2f_mixed_spy_qqq_adds_warning(self) -> None:
            from strategies.confirmation.market_regime import evaluate_market_regime
            spy = self.make_strategy_bars([100 + i * 0.12 for i in range(15)])
            qqq = self.make_strategy_bars([102 - i * 0.15 for i in range(15)])
            result = evaluate_market_regime({"SPY": spy, "QQQ": qqq}, load_config(None))
            self.assertEqual(result["market_regime"], "REVERSAL_ATTEMPT")
            self.assertTrue(any("disagree" in warning.lower() for warning in result["warnings"]))

        def test_phase4_market_regime_classifies_trending_up_and_down(self) -> None:
            from strategies.confirmation.market_regime import evaluate_market_regime
            up_spy = self.make_strategy_bars([100 + i * 0.12 for i in range(15)])
            up_qqq = self.make_strategy_bars([100 + i * 0.15 for i in range(15)])
            down_spy = self.make_strategy_bars([102 - i * 0.12 for i in range(15)])
            down_qqq = self.make_strategy_bars([102 - i * 0.15 for i in range(15)])
            for bars in (up_spy, up_qqq, down_spy, down_qqq):
                for bar in bars:
                    bar.t += timedelta(hours=2)
            up = evaluate_market_regime({"SPY": up_spy, "QQQ": up_qqq}, load_config(None), aapl_bars=up_qqq)
            down = evaluate_market_regime({"SPY": down_spy, "QQQ": down_qqq}, load_config(None), aapl_bars=down_qqq)
            self.assertEqual(up["market_regime"], "TRENDING_UP")
            self.assertEqual(down["market_regime"], "TRENDING_DOWN")
            self.assertIn("regime_reason", up)
            self.assertIn("regime_score", down)

        def test_phase4_market_regime_reports_spy_qqq_alignment_and_aapl_strength(self) -> None:
            from strategies.confirmation.market_regime import evaluate_market_regime
            aapl = self.make_strategy_bars([100 + i * 0.25 for i in range(15)])
            spy = self.make_strategy_bars([100 + i * 0.08 for i in range(15)])
            qqq = self.make_strategy_bars([100 + i * 0.10 for i in range(15)])
            result = evaluate_market_regime({"SPY": spy, "QQQ": qqq}, load_config(None), aapl_bars=aapl)
            self.assertEqual(result["spy_alignment"], "ALIGNED")
            self.assertEqual(result["qqq_alignment"], "ALIGNED")
            self.assertEqual(result["aapl_relative_strength"], "STRONG")
            self.assertIn(result["volume_state"], {"LOW", "NORMAL", "STRONG", "CLIMAX"})
            self.assertIn(result["volatility_state"], {"LOW", "NORMAL", "HIGH"})

        def test_phase4_market_regime_log_contains_context_only_symbols(self) -> None:
            temp_dir = Path(tempfile.mkdtemp())
            writer = AlertWriter(temp_dir / "alerts.csv", temp_dir / "alerts.jsonl")
            writer.market_regime_jsonl_path = temp_dir / "market_regime.jsonl"
            alert = self.make_phase3_heads_up_alert(
                market_regime="CHOPPY",
                regime_score=70,
                regime_reason="SPY/QQQ are repeatedly crossing VWAP",
                spy_alignment="NEUTRAL",
                qqq_alignment="NEUTRAL",
                aapl_relative_strength="NEUTRAL",
                volume_state="NORMAL",
                volatility_state="NORMAL",
            )
            writer.write(alert)
            record = json.loads(writer.market_regime_jsonl_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["symbol"], "AAPL")
            self.assertEqual(record["context_symbols"], ["SPY", "QQQ"])
            self.assertTrue(record["alert_symbol"])

        def test_phase5_multi_timeframe_detects_premarket_and_previous_day_levels(self) -> None:
            from strategies.context import evaluate_multi_timeframe_context
            bars = self.make_strategy_bars([100 + i * 0.05 for i in range(45)])
            prior = [
                Bar(t=bars[0].t - timedelta(days=1), o=98.0, h=103.0, l=97.0, c=101.0, v=5_000_000),
            ]
            result = evaluate_multi_timeframe_context(
                bars,
                daily_bars=prior,
                premarket_high=102.5,
                premarket_low=98.5,
            )
            self.assertEqual(result["levels"]["pmh"], 102.5)
            self.assertEqual(result["levels"]["pml"], 98.5)
            self.assertEqual(result["levels"]["pdh"], 103.0)
            self.assertEqual(result["levels"]["pdl"], 97.0)
            self.assertEqual(result["levels"]["pdc"], 101.0)

        def test_phase5_multi_timeframe_updates_hod_lod_and_nearest_level(self) -> None:
            from strategies.context import evaluate_multi_timeframe_context
            closes = [100 + i * 0.04 for i in range(45)]
            highs = [close + 0.15 for close in closes]
            lows = [close - 0.15 for close in closes]
            highs[-1] = 105.0
            lows[0] = 96.0
            result = evaluate_multi_timeframe_context(
                self.make_strategy_bars(closes, highs=highs, lows=lows),
                premarket_high=102.0,
                premarket_low=98.0,
            )
            self.assertEqual(result["levels"]["hod"], 105.0)
            self.assertEqual(result["levels"]["lod"], 96.0)
            self.assertIsNotNone(result["nearest_level_name"])
            self.assertIsNotNone(result["distance_to_key_level_pct"])

        def test_phase5_multi_timeframe_trend_classification_and_alignment_warning(self) -> None:
            from strategies.context import evaluate_multi_timeframe_context
            bullish = evaluate_multi_timeframe_context(self.make_strategy_bars([100 + i * 0.08 for i in range(60)]))
            self.assertEqual(bullish["trend_1m"], "BULLISH")
            self.assertEqual(bullish["trend_5m"], "BULLISH")
            self.assertEqual(bullish["current_bias"], "BULLISH")
            self.assertEqual(bullish["key_warning"], "")

        def test_phase5_multi_timeframe_log_preserves_aapl_alert_scope(self) -> None:
            temp_dir = Path(tempfile.mkdtemp())
            writer = AlertWriter(temp_dir / "alerts.csv", temp_dir / "alerts.jsonl")
            writer.multi_timeframe_jsonl_path = temp_dir / "multi_timeframe_context.jsonl"
            alert = self.make_phase3_heads_up_alert(
                trend_1m="BULLISH",
                trend_5m="BULLISH",
                trend_15m="BULLISH",
                current_structure_bias="BULLISH",
                nearest_level_name="VWAP",
                nearest_level_price=100.0,
            )
            writer.write(alert)
            record = json.loads(writer.multi_timeframe_jsonl_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["symbol"], "AAPL")
            self.assertTrue(record["alert_symbol"])
            self.assertEqual(record["context_symbols"], ["SPY", "QQQ"])

        def test_phase6_classifier_bullish_pullback_hold_and_bearish_rejection(self) -> None:
            from strategies.setup_classifier import classify_professional_setup
            bullish = classify_professional_setup(
                {"primary_setup": "VWAP Hold", "direction": "bullish", "confidence_score": 80},
                {"top_scenario": {"scenario_name": "Pullback Holding", "direction": "bullish", "stage": "CONFIRMED", "score": 84, "reasons": ["Higher low held"]}},
            )
            bearish = classify_professional_setup(
                {"primary_setup": "VWAP Rejection", "direction": "bearish", "confidence_score": 80},
                {"top_scenario": {"scenario_name": "Pullback Rejecting", "direction": "bearish", "stage": "GOOD_POSITION", "score": 86, "reasons": ["Underside retest rejected"]}},
            )
            self.assertEqual(bullish["setup_name"], "Bullish Pullback Holding")
            self.assertEqual(bullish["stage"], "CONFIRMED")
            self.assertEqual(bearish["setup_name"], "Bearish Pullback Rejecting")
            self.assertEqual(bearish["direction"], "bearish")

        def test_phase6_classifier_liquidity_sweep_reclaim_and_rejection(self) -> None:
            from strategies.setup_classifier import classify_professional_setup
            bullish = classify_professional_setup(
                {"primary_setup": "Bullish Liquidity Sweep Reclaim", "direction": "bullish", "confidence_score": 82},
                {},
            )
            bearish = classify_professional_setup(
                {"primary_setup": "Bearish Liquidity Sweep Rejection", "direction": "bearish", "confidence_score": 82},
                {},
            )
            self.assertEqual(bullish["setup_name"], "Bullish Liquidity Sweep Reclaim")
            self.assertEqual(bearish["setup_name"], "Bearish Liquidity Sweep Rejection")

        def test_phase6_classifier_failed_breakout(self) -> None:
            from strategies.setup_classifier import classify_professional_setup
            result = classify_professional_setup(
                {"primary_setup": "Possible Fakeout", "direction": "bearish", "confidence_score": 70},
                {"top_scenario": {"scenario_name": "Failed Breakout", "direction": "bearish", "stage": "CONFIRMED", "score": 78}},
            )
            self.assertEqual(result["setup_name"], "Bearish Failed Breakout")
            self.assertEqual(result["direction"], "bearish")

        def test_phase6_classifier_mixed_signal_explains_failed_bullish_sweep(self) -> None:
            from strategies.setup_classifier import classify_professional_setup
            result = classify_professional_setup(
                {"primary_setup": "Bullish Liquidity Sweep Reclaim", "direction": "bullish", "confidence_score": 82},
                {
                    "top_scenario": {"scenario_name": "Failed VWAP/EMA Reclaim", "direction": "bearish", "stage": "FORMING", "score": 72},
                    "scenario_conflict": True,
                },
            )
            self.assertEqual(result["setup_name"], "Mixed Signal")
            self.assertEqual(result["setup_code"], "MIXED_SIGNAL")
            self.assertTrue(result["mixed_signal"])
            self.assertIn("reclaim failed", result["reason"].lower())
            self.assertIn("MIXED_SIGNAL", result["block_reason"])

        def test_phase2f_choppy_market_lowers_setup_confidence(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.7, 99.9, 100.1, 100.4, 101.0],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            bull_spy = self.make_strategy_bars([100 + i * 0.12 for i in range(15)])
            bull_qqq = self.make_strategy_bars([100 + i * 0.15 for i in range(15)])
            choppy_closes = [100.0, 100.4, 99.7, 100.3, 99.8, 100.2, 99.7, 100.3, 99.8, 100.2, 99.7, 100.3, 99.8, 100.2, 99.9]
            choppy_spy = self.make_strategy_bars(choppy_closes)
            choppy_qqq = self.make_strategy_bars(choppy_closes)
            bull = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.0, market_bars={"SPY": bull_spy, "QQQ": bull_qqq})
            choppy = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.0, market_bars={"SPY": choppy_spy, "QQQ": choppy_qqq})
            self.assertEqual(choppy["market_regime"], "CHOPPY")
            self.assertLess(choppy["confidence_score"], bull["confidence_score"])

        def test_phase2g_trades_near_ask_create_buyer_pressure(self) -> None:
            from strategies.confirmation.pressure_score import evaluate_pressure_score
            data = {
                "quote": {"bid": 100.0, "ask": 100.05, "bid_size": 800, "ask_size": 300},
                "trades": [{"price": 100.04, "size": 100}, {"price": 100.05, "size": 120}, {"price": 100.04, "size": 90}],
            }
            result = evaluate_pressure_score(data, load_config(None), direction="bullish")
            self.assertEqual(result["pressure_label"], "BUYERS_ACTIVE")
            self.assertGreater(result["trade_near_ask_count"], result["trade_near_bid_count"])

        def test_phase2g_trades_near_bid_create_seller_pressure(self) -> None:
            from strategies.confirmation.pressure_score import evaluate_pressure_score
            data = {
                "quote": {"bid": 100.0, "ask": 100.1, "bid_size": 300, "ask_size": 900},
                "trades": [{"price": 100.0, "size": 100}, {"price": 100.01, "size": 130}, {"price": 100.02, "size": 110}],
            }
            result = evaluate_pressure_score(data, load_config(None), direction="bearish")
            self.assertEqual(result["pressure_label"], "SELLERS_ACTIVE")
            self.assertGreater(result["trade_near_bid_count"], result["trade_near_ask_count"])

        def test_phase2g_large_print_in_setup_direction_boosts_score(self) -> None:
            from strategies.confirmation.pressure_score import evaluate_pressure_score
            trades = [{"price": 100.09, "size": 100} for _ in range(10)] + [{"price": 100.1, "size": 1200}]
            data = {"quote": {"bid": 100.0, "ask": 100.1}, "trades": trades}
            result = evaluate_pressure_score(data, load_config(None), direction="bullish")
            self.assertGreaterEqual(result["large_print_count"], 1)
            self.assertTrue(any("large prints" in reason.lower() for reason in result["reasons"]))

        def test_phase2g_large_print_against_setup_adds_warning(self) -> None:
            from strategies.confirmation.pressure_score import evaluate_pressure_score
            trades = [{"price": 100.09, "size": 100} for _ in range(10)] + [{"price": 100.0, "size": 1200}]
            data = {"quote": {"bid": 100.0, "ask": 100.1}, "trades": trades}
            result = evaluate_pressure_score(data, load_config(None), direction="bullish")
            self.assertTrue(any("against" in warning.lower() for warning in result["warnings"]))

        def test_phase2g_missing_trade_quote_data_returns_unknown_safely(self) -> None:
            from strategies.confirmation.pressure_score import evaluate_pressure_score
            result = evaluate_pressure_score(None, load_config(None), direction="bullish")
            self.assertEqual(result["pressure_label"], "UNKNOWN")

        def test_phase2g_wide_spread_adds_warning(self) -> None:
            from strategies.confirmation.pressure_score import evaluate_pressure_score
            data = {
                "quote": {"bid": 100.0, "ask": 100.3},
                "trades": [{"price": 100.28, "size": 100}, {"price": 100.29, "size": 110}],
            }
            result = evaluate_pressure_score(data, load_config(None), direction="bullish")
            self.assertTrue(any("spread is wide" in warning.lower() for warning in result["warnings"]))

        def test_phase2h_strong_phase1_and_phase2_gives_high_confidence(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.8, 100.2, 100.6, 100.08, 100.14],
                highs=[99.7, 100.0, 100.4, 100.8, 100.25, 100.15],
                lows=[99.3, 99.6, 100.05, 100.3, 100.0, 100.02],
                volumes=[1000, 1000, 2500, 2800, 1500, 1600],
            )
            spy = self.make_strategy_bars([100 + i * 0.04 for i in range(15)])
            qqq = self.make_strategy_bars([100 + i * 0.04 for i in range(15)])
            summary = self.strategy_summary(bars, {"pmh": 100.0}, rel_vol=2.2, market_bars={"SPY": spy, "QQQ": qqq})
            self.assertEqual(summary["confidence_label"], "HIGH")
            self.assertIn(summary["confirmation_label"], {"NORMAL", "STRONG"})
            self.assertEqual(summary["entry_quality_label"], "GOOD_POSITION")

        def test_phase2h_strong_phase1_weak_volume_candle_lowers_confidence(self) -> None:
            good_bars = self.make_strategy_bars(
                [99.5, 99.7, 99.9, 100.1, 100.4, 101.0],
                highs=[99.7, 99.9, 100.1, 100.3, 100.6, 101.1],
                lows=[99.3, 99.5, 99.7, 99.9, 100.2, 100.0],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            weak_bars = self.make_strategy_bars(
                [99.5, 99.7, 99.9, 100.1, 100.4, 100.7],
                highs=[99.7, 99.9, 100.1, 100.3, 100.6, 101.8],
                lows=[99.3, 99.5, 99.7, 99.9, 100.2, 100.2],
                volumes=[2000, 2000, 2000, 2000, 2000, 500],
            )
            good = self.strategy_summary(good_bars, {"pmh": 100.5}, rel_vol=2.0)
            weak = self.strategy_summary(weak_bars, {"pmh": 100.5}, rel_vol=0.5)
            self.assertLess(weak["confidence_score"], good["confidence_score"])
            self.assertTrue(any("volume" in warning.lower() or "candle" in warning.lower() for warning in weak["warnings"]))

        def test_phase2h_pressure_unknown_stays_neutral(self) -> None:
            bars = self.make_strategy_bars([99.5, 99.7, 99.9, 100.1, 100.4, 101.0], volumes=[1000, 1000, 1000, 1000, 1200, 3500])
            summary = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.0)
            self.assertEqual(summary["pressure_label"], "UNKNOWN")
            self.assertEqual(summary["pressure_score"], 50)

        def test_phase2h_conflicting_signals_produce_warnings(self) -> None:
            bars = self.make_strategy_bars(
                [99.5, 99.7, 99.9, 100.1, 100.4, 100.8],
                volumes=[1000, 1000, 1000, 1000, 1200, 3500],
            )
            qqq = self.make_strategy_bars([100.0, 100.5, 101.0, 101.5, 102.0, 102.5])
            spy = self.make_strategy_bars([100.0, 100.3, 100.6, 100.9, 101.2, 101.5])
            summary = self.strategy_summary(bars, {"pmh": 100.5}, rel_vol=2.0, market_bars={"SPY": spy, "QQQ": qqq})
            self.assertTrue(summary["warnings"])
            self.assertTrue(any("relative strength" in warning.lower() or "market" in warning.lower() for warning in summary["warnings"]))

        def test_phase2h_alert_formatting_includes_compact_confirmation_fields(self) -> None:
            alert = Alert(
                symbol="AAPL",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=101.25,
                direction="BULLISH",
                primary_setup="Breakout Retest Holding",
                strategy_confidence_score=84,
                strategy_confidence_label="HIGH",
                confirmation_score=78,
                confirmation_label="STRONG",
                entry_quality_label="GOOD_POSITION",
                risk_label="MEDIUM",
                volume_label="STRONG",
                rvol_detail=2.1,
                candle_label="BUYER_CONTROL",
                relative_strength_label="STRONG",
                market_regime="BULL_TREND",
            )
            message = compact_alert_message(alert)
            self.assertIn("confirm 78 STRONG", message)
            self.assertIn("entry GOOD_POSITION", message)
            self.assertIn("RS STRONG", message)
            full = format_alert_message(alert)
            self.assertIn("Confirmation:", full)
            self.assertIn("Entry quality:", full)

        def test_cleanup_entry_quality_falls_back_to_early_for_active_setup(self) -> None:
            config = load_config(None)
            config["strategy_engine"]["volume_confirm_multiplier"] = 1.2
            config["strategy_engine"]["enable_retest_hold"] = False
            config["strategy_engine"]["max_extension_from_vwap_pct"] = 5.0
            config["strategy_engine"]["max_extension_from_ema9_pct"] = 5.0
            config["confirmation"]["extension_exhaustion"]["max_extension_from_vwap_pct"] = 5.0
            config["confirmation"]["extension_exhaustion"]["max_extension_from_ema9_pct"] = 5.0
            config["confirmation"]["extension_exhaustion"]["max_extension_from_key_level_pct"] = 5.0
            bars = self.make_strategy_bars(
                [99.5, 99.7, 99.9, 100.1, 100.3, 100.55],
                highs=[99.7, 99.9, 100.1, 100.3, 100.5, 100.65],
                lows=[99.3, 99.5, 99.7, 99.9, 100.1, 100.35],
                volumes=[1000, 1000, 1000, 1000, 1200, 2500],
            )
            summary = evaluate_strategy_suite("TEST", bars, bars[-1], config, {"pmh": 100.5}, 2.0, "ALIGNED")
            self.assertIsNotNone(summary["primary_setup"])
            self.assertEqual(summary["entry_quality_label"], "EARLY")

        def test_cleanup_warning_priority_orders_before_trimming(self) -> None:
            from strategies.scoring import _prioritized_warnings
            warnings = [
                "Everything else",
                "Spread is wide for pressure confirmation",
                "Small candle body shows indecision",
                "RVOL 0.90x is below confirmation threshold",
                "Fakeout Risk: broke above PMH but lost the level",
                "Bullish setup lacks relative strength",
                "Market regime is opposing the setup",
                "Do Not Chase: price is too far extended from VWAP",
            ]
            ordered = _prioritized_warnings(warnings)
            self.assertTrue(ordered[0].startswith("Do Not Chase"))
            self.assertIn("Market regime", ordered[1])
            self.assertIn("relative strength", ordered[2])
            self.assertIn("Fakeout", ordered[3])

        def test_cleanup_do_not_chase_risk_displays_loudly(self) -> None:
            alert = Alert(
                symbol="AAPL",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=101.25,
                direction="BULLISH",
                risk_label="DO_NOT_CHASE",
            )
            compact = compact_alert_message(alert)
            self.assertIn("RISK: DO_NOT_CHASE", compact)
            self.assertIn("valid setup may be late", compact)
            full = format_alert_message(alert)
            self.assertIn("RISK:", full)
            self.assertIn("DO_NOT_CHASE", full)

        def test_relative_volume(self) -> None:
            config = load_config(None)
            config["symbols"] = ["ASTS"]
            config["symbols_with_options"] = ["ASTS"]
            provider = MockProvider(["ASTS"])
            notifier = DiscordNotifier(None)
            writer = AlertWriter(LOG_DIR / "test_alerts.csv", LOG_DIR / "test_alerts.jsonl")
            state = StateStore(STATE_DIR / "test_state.json")
            scanner = EliteScanner(config, provider, notifier, writer, state)
            snaps = scanner.build_snapshots()
            snap = snaps["ASTS"]
            self.assertTrue(len(snap.recent_bars) > 5)
            rv = scanner.compute_relative_volume(snap.recent_bars)
            self.assertIsNotNone(rv)

        def test_alert_generation(self) -> None:
            config = load_config(None)
            config["symbols"] = ["ASTS"]
            config["symbols_with_options"] = ["ASTS"]
            config["fast_move_pct_threshold"] = 0.0
            config["relative_volume_threshold"] = 0.0
            provider = MockProvider(["ASTS"])
            notifier = DiscordNotifier(None)
            writer = AlertWriter(LOG_DIR / "test2_alerts.csv", LOG_DIR / "test2_alerts.jsonl")
            state = StateStore(STATE_DIR / "test2_state.json")
            scanner = EliteScanner(config, provider, notifier, writer, state)
            snaps = scanner.build_snapshots()
            alerts = scanner.evaluate_symbol(snaps["ASTS"])
            self.assertTrue(len(alerts) >= 1)

        def test_stale_snapshot_suppresses_alerts(self) -> None:
            config = load_config(None)
            config["symbols"] = ["ASTS"]
            config["symbols_with_options"] = ["ASTS"]
            config["fast_move_pct_threshold"] = 0.0
            config["relative_volume_threshold"] = 0.0
            old = now_utc() - timedelta(minutes=60)
            bars = [
                Bar(t=old + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=100.0 + i * 0.1, v=200000)
                for i in range(12)
            ]
            snap = SymbolSnapshot(symbol="ASTS", latest_bar=bars[-1], recent_bars=bars)
            scanner = EliteScanner(
                config,
                MockProvider(["ASTS"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_stale_alerts.csv", LOG_DIR / "test_stale_alerts.jsonl"),
                StateStore(STATE_DIR / "test_stale_state.json"),
            )
            self.assertEqual(snapshot_data_quality(snap, config), "Stale")
            self.assertEqual(scanner.evaluate_symbol(snap), [])

        def test_session_anchor_prefers_regular_open(self) -> None:
            config = load_config(None)
            pre = set_today_time_et(config["premarket_start"]).astimezone(UTC)
            open_t = set_today_time_et(config["market_open"]).astimezone(UTC)
            bars = [
                Bar(t=pre + timedelta(minutes=1), o=90.0, h=91.0, l=89.0, c=90.0, v=1000),
                Bar(t=open_t, o=100.0, h=101.0, l=99.0, c=100.0, v=1000),
                Bar(t=open_t + timedelta(minutes=1), o=101.0, h=102.0, l=100.0, c=101.0, v=1000),
            ]
            self.assertIs(session_anchor_bar(bars, config), bars[1])

        def test_premarket_levels_from_premarket_only(self) -> None:
            config = load_config(None)
            pre = set_today_time_et(config["premarket_start"]).astimezone(UTC)
            open_t = set_today_time_et(config["market_open"]).astimezone(UTC)
            bars = [
                Bar(t=pre + timedelta(minutes=1), o=10.0, h=12.0, l=9.0, c=11.0, v=1000),
                Bar(t=open_t, o=20.0, h=30.0, l=19.0, c=25.0, v=1000),
            ]
            pre_bars = [b for b in bars if is_premarket_bar(b.t, config)]
            self.assertEqual(max(b.h for b in pre_bars), 12.0)
            self.assertEqual(min(b.l for b in pre_bars), 9.0)

        def test_opening_range_requires_complete_bars(self) -> None:
            config = load_config(None)
            open_t = set_today_time_et(config["market_open"]).astimezone(UTC)
            incomplete = [
                Bar(t=open_t + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=100.0, v=1000)
                for i in range(int(config["opening_range_minutes"]) - 1)
            ]
            complete = incomplete + [
                Bar(t=open_t + timedelta(minutes=int(config["opening_range_minutes"]) - 1), o=100.0, h=101.0, l=99.0, c=100.0, v=1000)
            ]
            self.assertFalse(opening_range_complete(incomplete, config))
            self.assertTrue(opening_range_complete(complete, config))

        def option_contract(
            self,
            option_type: str = "C",
            expiration: Optional[date] = None,
            strike: float = 100.0,
            bid: float = 1.95,
            ask: float = 2.05,
            quote_time: Optional[datetime] = None,
            volume: int = 200,
            open_interest: int = 500,
            delta: Optional[float] = 0.45,
        ) -> OptionContractSnapshot:
            expiration = expiration or now_et().date()
            quote_time = quote_time or now_utc()
            if option_type == "P" and delta is not None and delta > 0:
                delta = -delta
            return OptionContractSnapshot(
                symbol=option_symbol("TEST", expiration, option_type, strike),
                underlying_symbol="TEST",
                option_type=option_type,
                expiration_date=expiration,
                strike=strike,
                bid=bid,
                ask=ask,
                quote_time=quote_time,
                volume=volume,
                open_interest=open_interest,
                delta=delta,
                implied_volatility=0.55,
            )

        def phase2_sms_fixture(self, direction: str = "BULLISH") -> tuple[EliteScanner, SymbolSnapshot, Alert]:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_phase2_sms_gate.csv", LOG_DIR / "test_phase2_sms_gate.jsonl"),
                StateStore(STATE_DIR / "test_phase2_sms_gate_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            if direction == "BEARISH":
                bars = [
                    Bar(t=start + timedelta(minutes=i), o=105.0 - i * 0.2, h=105.2 - i * 0.2, l=104.2 - i * 0.25, c=104.5 - i * 0.25, v=50000)
                    for i in range(12)
                ]
                snap = SymbolSnapshot(
                    symbol="TEST",
                    latest_bar=bars[-1],
                    recent_bars=bars,
                    premarket_low=102.5,
                    opening_range_low=102.0,
                    best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
                )
                alert = Alert(
                    symbol="TEST",
                    timestamp=now_utc(),
                    category="PREMARKET LOW BREAK",
                    price=101.75,
                    fast_move_pct=-1.1,
                    day_move_pct=-2.2,
                    relative_volume=2.1,
                    primary_setup="5-Min ORB Short",
                    strategy_direction="bearish",
                    strategy_confidence_score=92,
                    strategy_confidence_label="HIGH",
                    confirmation_score=72,
                    confirmation_label="STRONG",
                    risk_label="LOW",
                    entry_quality_label="GOOD_POSITION",
                    volume_label="STRONG",
                    candle_label="SELLER_CONTROL",
                    market_regime="BEAR_TREND",
                    relative_strength_label="NEUTRAL",
                    strategy_results=[{"active": True, "direction": "bearish", "score": 92, "label": "5-Min ORB Short"}],
                )
            else:
                bars = [
                    Bar(t=start + timedelta(minutes=i), o=100.0 + i * 0.2, h=101.0 + i * 0.2, l=99.0 + i * 0.1, c=101.0 + i * 0.25, v=50000)
                    for i in range(12)
                ]
                snap = SymbolSnapshot(
                    symbol="TEST",
                    latest_bar=bars[-1],
                    recent_bars=bars,
                    opening_range_high=103.2,
                    best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
                )
                alert = Alert(
                    symbol="TEST",
                    timestamp=now_utc(),
                    category="OPENING RANGE BREAK UP",
                    price=103.75,
                    fast_move_pct=1.1,
                    day_move_pct=2.2,
                    relative_volume=2.1,
                    primary_setup="5-Min ORB Long",
                    strategy_direction="bullish",
                    strategy_confidence_score=92,
                    strategy_confidence_label="HIGH",
                    confirmation_score=72,
                    confirmation_label="STRONG",
                    risk_label="LOW",
                    entry_quality_label="GOOD_POSITION",
                    volume_label="STRONG",
                    candle_label="BUYER_CONTROL",
                    market_regime="BULL_TREND",
                    relative_strength_label="NEUTRAL",
                    strategy_results=[{"active": True, "direction": "bullish", "score": 92, "label": "5-Min ORB Long"}],
                )
            return scanner, snap, alert

        def test_phase2_direction_conflict_caps_grade_and_blocks_sms(self) -> None:
            scanner, snap, alert = self.phase2_sms_fixture("BEARISH")
            alert.primary_setup = "Bullish Liquidity Sweep Reclaim"
            alert.strategy_direction = "bullish"
            alert.strategy_results = [{"active": True, "direction": "bullish", "score": 88, "label": alert.primary_setup}]
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BEARISH", "QQQ": "BEARISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertLessEqual(graded.alert_score or 0, 54)
            self.assertIn("Direction conflict", graded.text_alert_reason)

        def test_phase2_weak_confirmation_caps_a_grade(self) -> None:
            scanner, snap, alert = self.phase2_sms_fixture("BULLISH")
            alert.confirmation_score = 58
            alert.confirmation_label = "NORMAL"
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertLessEqual(graded.alert_score or 0, 69)
            self.assertNotEqual(graded.alert_grade, "A+")

        def test_phase2_high_and_do_not_chase_risk_block_sms(self) -> None:
            for risk in ("HIGH", "DO_NOT_CHASE"):
                scanner, snap, alert = self.phase2_sms_fixture("BULLISH")
                alert.risk_label = risk
                graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
                self.assertFalse(graded.sms_allowed)
                self.assertIn(f"Phase 2 risk is {risk}", graded.text_alert_reason)

        def test_phase2_choppy_unknown_market_blocks_sms_unless_strict_exception(self) -> None:
            scanner, snap, alert = self.phase2_sms_fixture("BULLISH")
            alert.market_regime = "CHOPPY"
            alert.confirmation_score = 69
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertIn("Market regime is CHOPPY", graded.text_alert_reason)

            scanner, snap, alert = self.phase2_sms_fixture("BULLISH")
            alert.market_regime = "UNKNOWN"
            alert.strategy_confidence_score = 95
            alert.confirmation_score = 72
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertTrue(graded.sms_allowed)

        def test_phase2_indecision_or_rejection_candle_blocks_sms(self) -> None:
            for candle in ("INDECISION", "REJECTION"):
                scanner, snap, alert = self.phase2_sms_fixture("BULLISH")
                alert.candle_label = candle
                graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
                self.assertFalse(graded.sms_allowed)
                self.assertIn(f"Candle quality is {candle}", graded.text_alert_reason)

        def test_aapl_bearish_continuation_pullback_rejecting_label(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["AAPL"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_aapl_bearish_continuation.csv", LOG_DIR / "test_aapl_bearish_continuation.jsonl"),
                StateStore(STATE_DIR / "test_aapl_bearish_continuation_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            closes = [316.0, 315.6, 315.0, 314.5, 314.0, 313.6, 313.2, 312.8, 312.5, 312.25, 312.05, 311.8]
            bars = [
                Bar(t=start + timedelta(minutes=i), o=close + 0.15, h=close + 0.30, l=close - 0.20, c=close, v=40000)
                for i, close in enumerate(closes)
            ]
            bars[-1] = Bar(t=now_utc(), o=312.25, h=312.62, l=311.55, c=311.8, v=70000)
            snap = SymbolSnapshot(
                symbol="AAPL",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_low=313.8,
                premarket_low=313.6,
                best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="AAPL",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK DOWN",
                price=311.8,
                fast_move_pct=-0.9,
                day_move_pct=-1.2,
                relative_volume=1.2,
                direction="BEARISH",
            )
            alert = scanner.attach_strategy_context(scanner.attach_option_context(alert, snap), snap, {"SPY": "BEARISH", "QQQ": "BEARISH"}, {"SPY": bars, "QQQ": bars})
            self.assertEqual(alert.primary_setup, "Bearish Trend Continuation - Pullback Rejecting")
            self.assertEqual(alert.entry_quality_label, "GOOD_POSITION")
            graded = scanner.grade_alert(alert, snap, {"SPY": "BEARISH", "QQQ": "BEARISH"})
            self.assertGreaterEqual(graded.confirmation_score or 0, 65)

        def test_repeated_orb_short_sms_requires_improved_confirmation(self) -> None:
            scanner, snap, first = self.phase2_sms_fixture("BEARISH")
            first.category = "OPENING RANGE BREAK DOWN"
            first.primary_setup = "5-Min ORB Short"
            first.confirmation_score = 65
            first.confirmation_label = "NORMAL"
            key = scanner.orb_sms_state_key(first)
            scanner.state_store.set_last_alert_time(key, now_utc())
            scanner.state_store.data.setdefault("orb_sms_confirmation_scores", {})[key] = 66
            graded = scanner.grade_alert(scanner.attach_option_context(first, snap), snap, {"SPY": "BEARISH", "QQQ": "BEARISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertIn("repeated ORB SMS blocked", graded.text_alert_reason)

            scanner, snap, improved = self.phase2_sms_fixture("BEARISH")
            improved.category = "OPENING RANGE BREAK DOWN"
            improved.primary_setup = "5-Min ORB Short"
            improved.confirmation_score = 72
            improved.confirmation_label = "STRONG"
            key = scanner.orb_sms_state_key(improved)
            scanner.state_store.set_last_alert_time(key, now_utc())
            scanner.state_store.data.setdefault("orb_sms_confirmation_scores", {})[key] = 66
            graded = scanner.grade_alert(scanner.attach_option_context(improved, snap), snap, {"SPY": "BEARISH", "QQQ": "BEARISH"})
            self.assertTrue(graded.sms_allowed)

        def test_options_selects_0dte_first(self) -> None:
            config = load_config(None)
            today = now_et().date()
            weekly = today + timedelta(days=7)
            chain = [
                self.option_contract(expiration=weekly, strike=100.0),
                self.option_contract(expiration=today, strike=100.0, bid=1.90, ask=2.00),
            ]
            selection = choose_best_option_contract(chain, "C", 100.0, config)
            self.assertEqual(selection.contract.expiration_date, today)
            self.assertEqual(selection.quality, "TRADABLE")

        def test_options_falls_back_to_nearest_weekly(self) -> None:
            config = load_config(None)
            weekly = now_et().date() + timedelta(days=7)
            selection = choose_best_option_contract([self.option_contract(expiration=weekly)], "C", 100.0, config)
            self.assertEqual(selection.contract.expiration_date, weekly)

        def test_options_rejects_wide_spread(self) -> None:
            config = load_config(None)
            selection = choose_best_option_contract([self.option_contract(bid=1.00, ask=1.50)], "C", 100.0, config)
            self.assertEqual(selection.quality, "WIDE_SPREAD")
            self.assertEqual(selection.score, 0)

        def test_options_rejects_stale_quote(self) -> None:
            config = load_config(None)
            stale = now_utc() - timedelta(minutes=10)
            selection = choose_best_option_contract([self.option_contract(quote_time=stale)], "C", 100.0, config)
            self.assertEqual(selection.quality, "STALE")

        def test_option_freshness_normalizes_utc_et_and_naive_timestamps(self) -> None:
            config = load_config(None)
            current = datetime(2026, 6, 8, 15, 0, tzinfo=UTC)
            utc_contract = self.option_contract(quote_time=current - timedelta(seconds=10))
            et_contract = self.option_contract(quote_time=(current - timedelta(seconds=10)).astimezone(ET))
            naive_contract = self.option_contract(quote_time=(current - timedelta(seconds=10)).replace(tzinfo=None))
            for contract in (utc_contract, et_contract, naive_contract):
                details = option_freshness_details(contract, config, current)
                self.assertEqual(details["status"], "recent")
                self.assertAlmostEqual(details["quote_age_seconds"], 10.0)

        def test_option_freshness_missing_timestamp_and_bid_ask_reasons(self) -> None:
            config = load_config(None)
            missing_time = self.option_contract()
            missing_time.quote_time = None
            self.assertEqual(option_freshness_details(missing_time, config)["stale_reason"], "missing_timestamp")
            zero_bid = self.option_contract(bid=0.0)
            details = option_freshness_details(zero_bid, config)
            self.assertEqual(details["status"], "invalid")
            self.assertEqual(details["stale_reason"], "")
            self.assertEqual(details["invalid_reason"], "missing_bid_or_ask")
            self.assertEqual(option_contract_quality(zero_bid, config)[0], "INVALID")

        def test_option_freshness_wide_spread_is_poor_quality_not_stale(self) -> None:
            config = load_config(None)
            details = option_freshness_details(self.option_contract(bid=1.0, ask=1.5), config)
            self.assertEqual(details["status"], "poor_quality")
            self.assertEqual(details["stale_reason"], "wide_spread")

        def test_extract_quote_timestamp_handles_objects_dicts_and_nested_quotes(self) -> None:
            class TimestampQuote:
                timestamp = "2026-06-08T15:00:00Z"

            class TQuote:
                t = "2026-06-08T15:00:01Z"

            cases = [
                (TimestampQuote(), "quote.timestamp"),
                (TQuote(), "quote.t"),
                ({"t": "2026-06-08T15:00:02Z"}, "quote.t"),
                ({"quotes": {"AAPLTEST": {"t": "2026-06-08T15:00:03Z"}}}, "quote.quotes.AAPLTEST.t"),
            ]
            for raw, expected_source in cases:
                extracted = extract_quote_timestamp(raw, "AAPLTEST")
                self.assertIsNotNone(extracted["quote_timestamp_utc"])
                self.assertEqual(extracted["timestamp_source_field"], expected_source)
                self.assertFalse(extracted["timestamp_extraction_failed"])

        def test_extract_quote_timestamp_handles_exact_alpaca_opra_dict(self) -> None:
            raw = {
                "ap": 0.04,
                "as": 4,
                "ax": "N",
                "bp": 0.03,
                "bs": 84,
                "bx": "W",
                "c": "A",
                "t": "2026-06-08T19:59:59.964593176Z",
            }
            extracted = extract_quote_timestamp(raw)
            self.assertEqual(extracted["timestamp_source_field"], "quote.t")
            self.assertEqual(extracted["quote_timestamp_raw"], raw["t"])
            self.assertEqual(
                extracted["quote_timestamp_utc"],
                datetime(2026, 6, 8, 19, 59, 59, 964593, tzinfo=UTC),
            )
            self.assertFalse(extracted["timestamp_extraction_failed"])

        def test_option_missing_quote_timestamp_recent_trade_is_diagnostic_only(self) -> None:
            config = load_config(None)
            contract = self.option_contract()
            contract.quote_time = None
            contract.trade_time = now_utc() - timedelta(seconds=5)
            contract.timestamp_extraction_failed = True
            contract.timestamp_fallback_type = "latest_trade"
            contract.timestamp_fallback_time = contract.trade_time
            details = option_freshness_details(contract, config)
            self.assertEqual(details["status"], "diagnostic")
            self.assertTrue(details["fallback_used"])
            self.assertEqual(details["fallback_type"], "latest_trade")
            self.assertEqual(option_contract_quality(contract, config)[0], "WATCH_ONLY")
            self.assertFalse(OptionSelection(contract, "Tradable diagnostic", 0).is_tradable())

        def test_option_missing_quote_timestamp_without_fallback_is_stale(self) -> None:
            config = load_config(None)
            contract = self.option_contract()
            contract.quote_time = None
            contract.trade_time = None
            details = option_freshness_details(contract, config)
            self.assertEqual(details["status"], "stale")
            self.assertEqual(details["stale_reason"], "missing_timestamp")

        def test_mixed_signal_and_news_are_explained_as_context_only(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                primary_setup="Bullish Liquidity Sweep Reclaim",
                strategy_direction="bullish",
                scenario_direction="bearish",
                scenario_conflict=True,
                scenario_top={"scenario_name": "Failed VWAP/EMA Reclaim", "direction": "bearish", "stage": "FORMING", "score": 70},
                headline="Fresh AAPL catalyst",
            )
            scanner.apply_mixed_signal_and_news_context(alert)
            self.assertTrue(alert.mixed_signal_detected)
            self.assertIn("Bullish sweep happened", alert.mixed_signal_reason)
            self.assertTrue(alert.conflict_warning_added)
            self.assertTrue(alert.news_context_present)
            self.assertTrue(alert.news_used_for_context_only)
            self.assertFalse(alert.news_upgraded_alert)

        def test_phase9_news_context_defaults_disabled_and_unavailable_does_not_crash(self) -> None:
            class NewsFailProvider(MockProvider):
                def get_news(self, symbols: List[str], limit: int = 50) -> List[NewsItem]:
                    raise RuntimeError("news unavailable")

            config = load_config(None)
            self.assertFalse(config["news_context"]["enabled"])
            scanner = EliteScanner(
                config,
                NewsFailProvider(["AAPL", "SPY", "QQQ"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_news_disabled.csv", LOG_DIR / "test_news_disabled.jsonl"),
                StateStore(STATE_DIR / "test_news_disabled_state.json"),
            )
            snapshots = scanner.build_snapshots()
            self.assertIsNone(snapshots["AAPL"].latest_news)

            config["news_context"]["enabled"] = True
            scanner = EliteScanner(
                config,
                NewsFailProvider(["AAPL", "SPY", "QQQ"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_news_unavailable.csv", LOG_DIR / "test_news_unavailable.jsonl"),
                StateStore(STATE_DIR / "test_news_unavailable_state.json"),
            )
            snapshots = scanner.build_snapshots()
            self.assertIsNone(snapshots["AAPL"].latest_news)

        def test_phase9_news_access_success_and_aapl_only_context(self) -> None:
            class NewsProvider(MockProvider):
                def get_news(self, symbols: List[str], limit: int = 50) -> List[NewsItem]:
                    self.requested_news_symbols = list(symbols)
                    return [
                        NewsItem(
                            symbol="AAPL",
                            headline="Apple raises outlook after strong growth",
                            url="https://example.com/aapl",
                            published_at=now_utc() - timedelta(minutes=10),
                            source="TestWire",
                        )
                    ]

            config = load_config(None)
            config["news_context"]["enabled"] = True
            provider = NewsProvider(["AAPL", "SPY", "QQQ"])
            scanner = EliteScanner(
                config,
                provider,
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_news_success.csv", LOG_DIR / "test_news_success.jsonl"),
                StateStore(STATE_DIR / "test_news_success_state.json"),
            )
            snapshots = scanner.build_snapshots()
            self.assertEqual(provider.requested_news_symbols, ["AAPL"])
            self.assertEqual(snapshots["AAPL"].latest_news.source, "TestWire")
            self.assertIsNone(snapshots["SPY"].latest_news)

        def test_phase9_news_cannot_upgrade_or_override_risk_or_options(self) -> None:
            scanner = self.make_phase3_heads_up_scanner()
            alert = self.make_phase3_heads_up_alert(
                headline="Apple raises outlook after strong growth",
                risk_label="DO_NOT_CHASE",
                option_quality="WIDE_SPREAD",
                option_tradable=False,
                sms_allowed=False,
                alert_grade="C",
                alert_score=45,
            )
            before = (alert.sms_allowed, alert.alert_grade, alert.alert_score, alert.risk_label, alert.option_quality)
            scanner.apply_mixed_signal_and_news_context(alert)
            after = (alert.sms_allowed, alert.alert_grade, alert.alert_score, alert.risk_label, alert.option_quality)
            self.assertEqual(before, after)
            self.assertTrue(alert.news_context_present)
            self.assertTrue(alert.news_used_for_context_only)
            self.assertFalse(alert.news_upgraded_alert)
            self.assertEqual(alert.news_sentiment_guess, "POSITIVE")
            self.assertIn("context only. Confirm price reaction", professional_telegram_message(alert, "PHASE3_HEADS_UP"))

        def test_options_rejects_delta_outside_range(self) -> None:
            config = load_config(None)
            selection = choose_best_option_contract([self.option_contract(delta=0.90)], "C", 100.0, config)
            self.assertEqual(selection.quality, "POOR_QUALITY")

        def test_option_quality_standard_labels_and_stock_only_behavior(self) -> None:
            config = load_config(None)
            tradable = evaluate_option_quality(self.option_contract(bid=1.95, ask=2.02), config, underlying_price=100.0)
            wide = evaluate_option_quality(self.option_contract(bid=1.00, ask=1.50), config, underlying_price=100.0)
            stale = evaluate_option_quality(
                self.option_contract(quote_time=now_utc() - timedelta(minutes=10)),
                config,
                underlying_price=100.0,
            )
            self.assertEqual(tradable["label"], "TRADABLE")
            self.assertTrue(tradable["trade_ready_allowed"])
            self.assertEqual(wide["label"], "WIDE_SPREAD")
            self.assertFalse(wide["trade_ready_allowed"])
            self.assertTrue(wide["stock_only_allowed"])
            self.assertEqual(wide["message"], "Option wide spread — stock setup only")
            self.assertEqual(stale["label"], "STALE")
            self.assertTrue(stale["stock_only_allowed"])
            self.assertEqual(stale["message"], "Option stale — stock setup only")

        def test_missing_timestamp_is_stale_and_trade_ready_blocked(self) -> None:
            config = load_config(None)
            contract = self.option_contract()
            contract.quote_time = None
            contract.trade_time = None
            result = evaluate_option_quality(contract, config, underlying_price=100.0)
            self.assertEqual(result["label"], "STALE")
            self.assertFalse(result["trade_ready_allowed"])
            self.assertTrue(result["stock_only_allowed"])

        def test_phase7_poor_option_blocks_existing_trade_ready_sms(self) -> None:
            scanner, snap, alert = self.phase2_sms_fixture("BULLISH")
            snap.best_call = OptionSelection(
                self.option_contract("C", bid=1.00, ask=1.50),
                "WIDE_SPREAD",
                0,
                ["wide_spread"],
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertFalse(graded.option_tradable)
            self.assertTrue(graded.option_stock_only_allowed)

        def test_phase8_tracks_all_intervals_mfe_mae_and_direction(self) -> None:
            alert_time = datetime(2026, 6, 9, 14, 0, tzinfo=UTC)
            record = {
                "alert_timestamp": alert_time.isoformat(),
                "price_at_alert": 100.0,
                "direction": "BULLISH",
                "entry_timing_at_alert": "EARLY",
                "invalidation_level": 99.5,
                "target_move_pct": 0.30,
                "interval_prices": {},
                "interval_moves_pct": {},
                "hit_invalidation": False,
            }
            closes = {1: 99.9, 3: 100.2, 5: 100.4, 10: 100.7, 15: 100.9}
            bars = [
                Bar(
                    t=alert_time + timedelta(minutes=minute),
                    o=100.0,
                    h=close + 0.2,
                    l=99.4 if minute == 1 else close - 0.1,
                    c=close,
                    v=1000,
                )
                for minute, close in closes.items()
            ]
            updated = update_performance_record(record, bars, now=alert_time + timedelta(minutes=15))
            self.assertEqual(list(updated["interval_prices"]), ["1m", "3m", "5m", "10m", "15m"])
            self.assertAlmostEqual(updated["max_favorable_excursion_pct"], 1.1)
            self.assertAlmostEqual(updated["max_adverse_excursion_pct"], 0.6)
            self.assertTrue(updated["direction_correct"])
            self.assertTrue(updated["alert_was_early"])
            self.assertTrue(updated["hit_invalidation"])
            self.assertTrue(updated["hit_target_zone"])
            self.assertEqual(updated["status"], "COMPLETE")

        def test_phase8_daily_review_report_generated(self) -> None:
            from tools.review_alert_performance import build_report, summarize

            records = [
                {
                    "alert_timestamp": "2026-06-09T14:00:00+00:00",
                    "symbol": "AAPL",
                    "setup_type": "Bullish Pullback Holding",
                    "alert_tier": "SETUP_CONFIRMED",
                    "direction_correct": True,
                    "useful_alert": True,
                    "alert_was_late": False,
                    "should_be_blocked_next_time": False,
                    "interval_moves_pct": {"15m": 0.8},
                    "max_favorable_excursion_pct": 1.0,
                    "max_adverse_excursion_pct": 0.1,
                },
                {
                    "alert_timestamp": "2026-06-09T15:00:00+00:00",
                    "symbol": "AAPL",
                    "setup_type": "Late Move",
                    "alert_tier": "RISK_WARNING",
                    "direction_correct": False,
                    "useful_alert": False,
                    "alert_was_late": True,
                    "should_be_blocked_next_time": True,
                    "interval_moves_pct": {"15m": -0.5},
                    "max_favorable_excursion_pct": 0.1,
                    "max_adverse_excursion_pct": 0.7,
                },
            ]
            summary = summarize(records)
            report = build_report("2026-06-09", records)
            self.assertEqual(summary["best_setup_type"], "Bullish Pullback Holding")
            self.assertEqual(summary["noisiest_alert_tier"], "RISK_WARNING")
            self.assertIn("Alerts That Should Be Blocked Next Time", report)
            self.assertIn("Bullish Pullback Holding", report)

        def test_phase8_tracker_logs_alert_and_completed_intervals(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                tracker = PostAlertPerformanceTracker(root / "post_alert_performance.jsonl", root / "pending.json")
                alert_time = datetime(2026, 6, 9, 14, 0, tzinfo=UTC)
                alert = Alert(
                    symbol="AAPL",
                    timestamp=alert_time,
                    category="WATCH AAPL BULLISH",
                    price=100.0,
                    direction="BULLISH",
                    primary_setup="Bullish Pullback Holding",
                    alert_tier="SETUP_CONFIRMED",
                    market_regime="TRENDING_UP",
                    option_quality="TRADABLE",
                    invalidation_level=99.5,
                )
                registered = tracker.register(alert)
                bars = [
                    Bar(
                        t=alert_time + timedelta(minutes=minute),
                        o=100.0,
                        h=100.0 + minute * 0.1,
                        l=99.9,
                        c=100.0 + minute * 0.08,
                        v=1000,
                    )
                    for minute in (1, 3, 5, 10, 15)
                ]
                tracker.update(
                    {"AAPL": SymbolSnapshot(symbol="AAPL", recent_bars=bars, latest_bar=bars[-1])},
                    now=alert_time + timedelta(minutes=15),
                )
                lines = (root / "post_alert_performance.jsonl").read_text(encoding="utf-8").splitlines()
                completed = json.loads(lines[-1])
                self.assertEqual(completed["alert_id"], registered["alert_id"])
                self.assertEqual(completed["status"], "COMPLETE")
                self.assertEqual(len(completed["interval_prices"]), 5)
                self.assertFalse(tracker.pending)

        def test_directional_alert_prefers_call_or_put(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_options_alerts.csv", LOG_DIR / "test_options_alerts.jsonl"),
                StateStore(STATE_DIR / "test_options_state.json"),
            )
            call = OptionSelection(self.option_contract("C"), "Tradable", 90)
            put = OptionSelection(self.option_contract("P"), "Tradable", 88)
            snap = SymbolSnapshot(symbol="TEST", best_call=call, best_put=put)
            bullish = Alert(symbol="TEST", timestamp=now_utc(), category="OPENING RANGE BREAK UP", price=100, fast_move_pct=1.0)
            bearish = Alert(symbol="TEST", timestamp=now_utc(), category="OPENING RANGE BREAK DOWN", price=100, fast_move_pct=-1.0)
            bearish_with_green_fast_move = Alert(symbol="TEST", timestamp=now_utc(), category="OPENING RANGE BREAK DOWN", price=100, fast_move_pct=1.0)
            self.assertEqual(scanner.attach_option_context(bullish, snap).option_type, "CALL")
            self.assertEqual(scanner.attach_option_context(bearish, snap).option_type, "PUT")
            self.assertEqual(scanner.attach_option_context(bearish_with_green_fast_move, snap).option_type, "PUT")

        def test_generic_momentum_uses_price_direction(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_direction_alerts.csv", LOG_DIR / "test_direction_alerts.jsonl"),
                StateStore(STATE_DIR / "test_direction_state.json"),
            )
            call = OptionSelection(self.option_contract("C"), "Tradable", 90)
            put = OptionSelection(self.option_contract("P"), "Tradable", 88)
            snap = SymbolSnapshot(symbol="TEST", best_call=call, best_put=put)
            runner = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="CATALYST RUNNER",
                price=602.69,
                fast_move_pct=-0.18,
                day_move_pct=-4.25,
                relative_volume=4.73,
            )
            high_rvol = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="HIGH RELATIVE VOLUME",
                price=602.69,
                fast_move_pct=-0.18,
                day_move_pct=-4.25,
                relative_volume=4.73,
            )
            self.assertEqual(scanner.infer_alert_direction(runner), "BEARISH")
            self.assertEqual(scanner.attach_option_context(runner, snap).option_type, "PUT")
            self.assertEqual(scanner.infer_alert_direction(high_rvol), "BEARISH")
            self.assertEqual(scanner.attach_option_context(high_rvol, snap).option_type, "PUT")

        def test_low_quality_breakout_does_not_allow_text_alert(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_low_quality_alerts.csv", LOG_DIR / "test_low_quality_alerts.jsonl"),
                StateStore(STATE_DIR / "test_low_quality_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=100.2, v=20000)
                for i in range(12)
            ]
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=100.0,
                best_call=OptionSelection(self.option_contract("C"), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=100.2,
                fast_move_pct=0.1,
                day_move_pct=0.2,
                relative_volume=0.8,
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertIn(graded.alert_grade, {"Avoid", "C"})

        def test_strong_bullish_breakout_allows_text_alert(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_strong_quality_alerts.csv", LOG_DIR / "test_strong_quality_alerts.jsonl"),
                StateStore(STATE_DIR / "test_strong_quality_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0 + i * 0.2, h=101.0 + i * 0.2, l=99.0, c=101.0 + i * 0.25, v=30000)
                for i in range(12)
            ]
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=103.2,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=103.75,
                fast_move_pct=1.1,
                day_move_pct=2.2,
                relative_volume=2.1,
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertTrue(graded.sms_allowed)
            self.assertIn(graded.alert_grade, {"B", "A", "A+"})

        def test_phase2_direction_conflict_blocks_text_alert(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_phase2_direction_conflict_alerts.csv", LOG_DIR / "test_phase2_direction_conflict_alerts.jsonl"),
                StateStore(STATE_DIR / "test_phase2_direction_conflict_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0 - i * 0.1, h=101.0, l=98.0 - i * 0.2, c=99.0 - i * 0.25, v=50000)
                for i in range(12)
            ]
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                premarket_low=99.0,
                best_put=OptionSelection(self.option_contract("P", bid=2.0, ask=2.04), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="PREMARKET LOW BREAK",
                price=96.25,
                fast_move_pct=-1.1,
                day_move_pct=-2.2,
                relative_volume=2.1,
                primary_setup="Bullish Liquidity Sweep Reclaim",
                strategy_direction="bullish",
                strategy_confidence_score=88,
                strategy_confidence_label="HIGH",
                confirmation_score=78,
                confirmation_label="STRONG",
                risk_label="LOW",
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BEARISH", "QQQ": "BEARISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertIn("Phase 2 setup direction conflicts with alert direction", graded.text_alert_reason)

        def test_phase2_weak_confirmation_or_high_risk_blocks_text_alert(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_phase2_weak_confirmation_alerts.csv", LOG_DIR / "test_phase2_weak_confirmation_alerts.jsonl"),
                StateStore(STATE_DIR / "test_phase2_weak_confirmation_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0 + i * 0.2, h=101.0 + i * 0.2, l=99.0, c=101.0 + i * 0.25, v=50000)
                for i in range(12)
            ]
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=103.2,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=103.75,
                fast_move_pct=1.1,
                day_move_pct=2.2,
                relative_volume=2.1,
                primary_setup="5-Min ORB Long",
                strategy_direction="bullish",
                strategy_confidence_score=88,
                strategy_confidence_label="HIGH",
                confirmation_score=44,
                confirmation_label="WEAK",
                risk_label="HIGH",
                strategy_warnings=["Candle quality contradicts bullish setup"],
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertIn("Phase 2 has conflicting confirmation warnings", graded.text_alert_reason)

        def test_strong_fast_break_allows_near_threshold_rvol_with_unknown_market(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_near_rvol_break_alerts.csv", LOG_DIR / "test_near_rvol_break_alerts.jsonl"),
                StateStore(STATE_DIR / "test_near_rvol_break_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0 + i * 0.2, h=101.0 + i * 0.2, l=99.0, c=101.0 + i * 0.25, v=30000)
                for i in range(12)
            ]
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=103.2,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=103.55,
                fast_move_pct=1.1,
                day_move_pct=0.3,
                relative_volume=1.45,
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "UNKNOWN", "QQQ": "UNKNOWN"})
            self.assertTrue(graded.sms_allowed)
            self.assertIn(graded.alert_grade, {"B", "A", "A+"})

        def test_weak_rvol_break_still_does_not_text_alert(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_weak_rvol_break_alerts.csv", LOG_DIR / "test_weak_rvol_break_alerts.jsonl"),
                StateStore(STATE_DIR / "test_weak_rvol_break_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0 + i * 0.2, h=101.0 + i * 0.2, l=99.0, c=101.0 + i * 0.25, v=30000)
                for i in range(12)
            ]
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=103.2,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=103.55,
                fast_move_pct=1.1,
                day_move_pct=0.3,
                relative_volume=1.05,
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "UNKNOWN", "QQQ": "UNKNOWN"})
            self.assertFalse(graded.sms_allowed)
            self.assertIn("RVOL is only moderate", graded.text_alert_reason)

        def test_fast_clean_break_can_alert_before_hold_confirmation(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_fast_clean_break_alerts.csv", LOG_DIR / "test_fast_clean_break_alerts.jsonl"),
                StateStore(STATE_DIR / "test_fast_clean_break_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=102.0, l=99.0, c=101.8, v=30000)
                for i in range(11)
            ]
            bars.append(Bar(t=start + timedelta(minutes=11), o=101.9, h=102.5, l=101.8, c=102.35, v=45000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=102.0,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=102.35,
                fast_move_pct=0.35,
                day_move_pct=1.8,
                relative_volume=1.7,
            )
            enriched = scanner.attach_option_context(alert, snap)
            graded = scanner.grade_alert(enriched, snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertFalse(scanner.breakout_hold_ok(graded, snap))
            self.assertTrue(scanner.immediate_break_ok(graded, snap))
            self.assertTrue(graded.sms_allowed)
            self.assertIn("fast clean-break", graded.text_alert_reason)

        def test_sms_upgrade_can_bypass_category_cooldown_after_non_text_alert(self) -> None:
            config = load_config(None)
            with tempfile.TemporaryDirectory() as temp_dir:
                scanner = EliteScanner(
                    config,
                    MockProvider(["TEST"]),
                    DiscordNotifier(None),
                    AlertWriter(Path(temp_dir) / "upgrade_alerts.csv", Path(temp_dir) / "upgrade_alerts.jsonl"),
                    StateStore(Path(temp_dir) / "upgrade_state.json"),
                )
                first = Alert(
                    symbol="TEST",
                    timestamp=now_utc(),
                    category="OPENING RANGE BREAK UP",
                    price=102.2,
                    fast_move_pct=0.2,
                    day_move_pct=0.4,
                    relative_volume=1.0,
                    alert_grade="C",
                    alert_score=40,
                    sms_allowed=False,
                    watch_allowed=False,
                    setup_level="ALERT",
                    text_alert_reason="below text-alert threshold",
                )
                second = Alert(
                    symbol="TEST",
                    timestamp=now_utc(),
                    category="OPENING RANGE BREAK UP",
                    price=103.0,
                    fast_move_pct=1.1,
                    day_move_pct=0.8,
                    relative_volume=1.45,
                    alert_grade="A",
                    alert_score=84,
                    sms_allowed=True,
                    watch_allowed=False,
                    setup_level="ALERT",
                    text_alert_reason="passed checks",
                )
                self.assertTrue(scanner.process_alert(first))
                self.assertTrue(scanner.process_alert(second))
                self.assertTrue(any("text alert upgrade" in note for note in second.notes))
                self.assertIsNotNone(scanner.state_store.get_last_alert_time(scanner.text_cooldown_key(second, "SMS")))

        def test_extended_breakout_does_not_text_alert(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_extended_break_alerts.csv", LOG_DIR / "test_extended_break_alerts.jsonl"),
                StateStore(STATE_DIR / "test_extended_break_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=104.0, l=99.0, c=103.3, v=30000)
                for i in range(12)
            ]
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=102.0,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK UP",
                price=103.3,
                fast_move_pct=1.0,
                day_move_pct=2.2,
                relative_volume=2.5,
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertFalse(graded.sms_allowed)
            self.assertIn("break already extended", graded.text_alert_reason)

        def test_opening_range_watch_is_not_text_alert(self) -> None:
            config = load_config(None)
            config["opening_range_minutes"] = 0
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_or_watch_alerts.csv", LOG_DIR / "test_or_watch_alerts.jsonl"),
                StateStore(STATE_DIR / "test_or_watch_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=102.0, l=99.0, c=101.6, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=101.7, h=102.0, l=101.6, c=101.93, v=26000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=102.0,
                opening_range_low=99.0,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH OPENING RANGE BREAK UP")
            self.assertEqual(watch.setup_level, "WATCH")
            self.assertTrue(watch.watch_allowed)
            self.assertFalse(watch.sms_allowed)

        def test_opening_range_watch_can_fire_before_full_rvol_confirmation(self) -> None:
            config = load_config(None)
            config["opening_range_minutes"] = 0
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_early_or_watch_alerts.csv", LOG_DIR / "test_early_or_watch_alerts.jsonl"),
                StateStore(STATE_DIR / "test_early_or_watch_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=100.2, l=99.0, c=99.6, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=99.35, h=99.4, l=99.04, c=99.06, v=10000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=100.2,
                opening_range_low=99.0,
                best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, None)
            watch = next(alert for alert in alerts if alert.category == "WATCH OPENING RANGE BREAK DOWN")
            self.assertLess(watch.relative_volume or 0, config["alert_quality"]["watch_min_rvol"])
            self.assertTrue(watch.watch_allowed)
            self.assertFalse(watch.sms_allowed)

        def test_premarket_watch_is_not_text_alert(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_premarket_watch_alerts.csv", LOG_DIR / "test_premarket_watch_alerts.jsonl"),
                StateStore(STATE_DIR / "test_premarket_watch_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=100.4, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=100.5, h=100.9, l=100.4, c=100.88, v=26000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                premarket_high=101.0,
                premarket_low=99.0,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH PREMARKET HIGH BREAK")
            self.assertEqual(watch.trigger_level, 101.0)
            self.assertTrue(watch.watch_allowed)
            self.assertFalse(watch.sms_allowed)

        def test_bearish_watch_can_warn_when_market_opposed_if_stock_specific(self) -> None:
            config = load_config(None)
            config["opening_range_minutes"] = 0
            config["alert_quality"]["fast_impulse_watch_enabled"] = False
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_watch_opposed_alerts.csv", LOG_DIR / "test_watch_opposed_alerts.jsonl"),
                StateStore(STATE_DIR / "test_watch_opposed_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=99.6 - i * 0.02, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=99.3, h=99.4, l=99.0, c=99.05, v=26000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=101.0,
                opening_range_low=99.0,
                best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH OPENING RANGE BREAK DOWN")
            self.assertEqual(watch.market_alignment, "OPPOSED")
            self.assertTrue(watch.watch_allowed)
            self.assertFalse(watch.sms_allowed)
            self.assertIn("bearish stock-specific move", watch.text_alert_reason)

        def test_bullish_watch_still_blocks_opposed_market_read(self) -> None:
            config = load_config(None)
            config["opening_range_minutes"] = 0
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_bullish_watch_opposed_alerts.csv", LOG_DIR / "test_bullish_watch_opposed_alerts.jsonl"),
                StateStore(STATE_DIR / "test_bullish_watch_opposed_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=100.0 + i * 0.02, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=100.6, h=100.92, l=100.5, c=100.88, v=26000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=101.0,
                opening_range_low=99.0,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, {"SPY": "BEARISH", "QQQ": "BEARISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH OPENING RANGE BREAK UP")
            self.assertEqual(watch.market_alignment, "OPPOSED")
            self.assertFalse(watch.watch_allowed)

        def test_opposed_bearish_watch_blocks_low_rvol_drift(self) -> None:
            config = load_config(None)
            config["opening_range_minutes"] = 0
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_opposed_bearish_low_rvol.csv", LOG_DIR / "test_opposed_bearish_low_rvol.jsonl"),
                StateStore(STATE_DIR / "test_opposed_bearish_low_rvol_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=99.6 - i * 0.02, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=99.3, h=99.4, l=99.0, c=99.05, v=5000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=101.0,
                opening_range_low=99.0,
                best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH OPENING RANGE BREAK DOWN")
            self.assertFalse(watch.watch_allowed)

        def test_opposed_bearish_watch_blocks_weak_put_option(self) -> None:
            config = load_config(None)
            config["opening_range_minutes"] = 0
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_opposed_bearish_weak_put.csv", LOG_DIR / "test_opposed_bearish_weak_put.jsonl"),
                StateStore(STATE_DIR / "test_opposed_bearish_weak_put_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=99.6 - i * 0.02, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=99.3, h=99.4, l=99.0, c=99.05, v=26000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=101.0,
                opening_range_low=99.0,
                best_put=OptionSelection(self.option_contract("P", bid=1.00, ask=1.25), "Wide spread", 40),
            )
            alerts = scanner.evaluate_symbol(snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH OPENING RANGE BREAK DOWN")
            self.assertFalse(watch.watch_allowed)

        def test_opposed_bearish_alert_can_downgrade_to_watch_only(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_opposed_bearish_downgrade.csv", LOG_DIR / "test_opposed_bearish_downgrade.jsonl"),
                StateStore(STATE_DIR / "test_opposed_bearish_downgrade_state.json"),
            )
            now = now_utc()
            bars = [
                Bar(t=now - timedelta(minutes=12 - i), o=100.0, h=100.3, l=99.5, c=100.0 - i * 0.08, v=20000)
                for i in range(11)
            ]
            latest = Bar(t=now, o=99.0, h=99.1, l=98.35, c=98.4, v=26000)
            bars.append(latest)
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=latest,
                recent_bars=bars,
                opening_range_high=101.0,
                opening_range_low=99.0,
                best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now,
                category="OPENING RANGE BREAK DOWN",
                price=98.4,
                fast_move_pct=-0.24,
                day_move_pct=-1.2,
                relative_volume=1.0,
                setup_level="ALERT",
                trigger_level=99.0,
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertEqual(graded.market_alignment, "OPPOSED")
            self.assertFalse(graded.sms_allowed)
            self.assertTrue(graded.watch_allowed)
            self.assertIn("bearish stock-specific move", graded.text_alert_reason)

        def test_bullish_reversal_watch_after_recent_bearish_watch(self) -> None:
            config = load_config(None)
            config["alert_quality"]["fast_impulse_watch_enabled"] = False
            with tempfile.TemporaryDirectory() as temp_dir:
                state = StateStore(Path(temp_dir) / "reversal_state.json")
                state.set_last_alert_time(f"{now_et().strftime('%Y-%m-%d')}:WATCH:TEST:BEARISH", now_utc())
                scanner = EliteScanner(
                    config,
                    MockProvider(["TEST"]),
                    DiscordNotifier(None),
                    AlertWriter(Path(temp_dir) / "reversal_alerts.csv", Path(temp_dir) / "reversal_alerts.jsonl"),
                    state,
                )
                start = now_utc() - timedelta(minutes=11)
                bars = [
                    Bar(t=start + timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=100.0 + i * 0.02, v=20000)
                    for i in range(11)
                ]
                bars.append(Bar(t=now_utc(), o=100.2, h=100.9, l=100.1, c=100.85, v=26000))
                snap = SymbolSnapshot(
                    symbol="TEST",
                    latest_bar=bars[-1],
                    recent_bars=bars,
                    best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
                )
                alerts = scanner.evaluate_symbol(snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH REVERSAL UP")
            self.assertTrue(watch.watch_allowed)
            self.assertEqual(watch.direction, "BULLISH")
            self.assertIn("reversal after recent opposite watch", watch.notes)

        def test_same_scan_watch_favors_bullish_latest_move(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_same_scan_bullish_watch.csv", LOG_DIR / "test_same_scan_bullish_watch.jsonl"),
                StateStore(STATE_DIR / "test_same_scan_bullish_watch_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=100.4, h=101.0, l=100.3, c=100.4 + i * 0.03, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=100.55, h=100.9, l=100.5, c=100.85, v=26000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                premarket_high=101.0,
                premarket_low=100.7,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
                best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, None)
            bullish = next(alert for alert in alerts if alert.category == "WATCH PREMARKET HIGH BREAK")
            bearish = next(alert for alert in alerts if alert.category == "WATCH PREMARKET LOW BREAK")
            self.assertEqual(alerts.index(bullish), 0)
            self.assertTrue(bullish.watch_allowed)
            self.assertFalse(bearish.watch_allowed)
            self.assertIn("latest fast move opposes setup direction", bearish.text_alert_reason)

        def test_same_scan_watch_favors_bearish_latest_move(self) -> None:
            config = load_config(None)
            config["alert_quality"]["fast_impulse_watch_enabled"] = False
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_same_scan_bearish_watch.csv", LOG_DIR / "test_same_scan_bearish_watch.jsonl"),
                StateStore(STATE_DIR / "test_same_scan_bearish_watch_state.json"),
            )
            start = now_utc() - timedelta(minutes=11)
            bars = [
                Bar(t=start + timedelta(minutes=i), o=101.2, h=101.4, l=100.8, c=101.2 - i * 0.02, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now_utc(), o=101.0, h=101.05, l=100.8, c=100.85, v=26000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                premarket_high=101.0,
                premarket_low=100.7,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
                best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, None)
            bearish = next(alert for alert in alerts if alert.category == "WATCH PREMARKET LOW BREAK")
            bullish = next(alert for alert in alerts if alert.category == "WATCH PREMARKET HIGH BREAK")
            self.assertEqual(alerts.index(bearish), 0)
            self.assertTrue(bearish.watch_allowed)
            self.assertFalse(bullish.watch_allowed)
            self.assertIn("latest fast move opposes setup direction", bullish.text_alert_reason)

        def test_failed_opening_range_breakout_creates_bearish_watch(self) -> None:
            config = load_config(None)
            config["alert_quality"]["fast_impulse_watch_enabled"] = False
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_failed_breakout_watch.csv", LOG_DIR / "test_failed_breakout_watch.jsonl"),
                StateStore(STATE_DIR / "test_failed_breakout_watch_state.json"),
            )
            start = now_utc() - timedelta(minutes=12)
            closes = [100.1, 100.2, 100.35, 100.55, 100.78, 101.15, 101.4, 101.2, 101.05, 100.85, 100.72]
            bars = [
                Bar(t=start + timedelta(minutes=i), o=close - 0.05, h=close + 0.08, l=close - 0.12, c=close, v=20000)
                for i, close in enumerate(closes)
            ]
            bars.append(Bar(t=now_utc(), o=100.68, h=100.76, l=100.45, c=100.55, v=26000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                opening_range_high=100.7,
                opening_range_low=99.2,
                best_put=OptionSelection(self.option_contract("P", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, {"SPY": "BEARISH", "QQQ": "BEARISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH FAILED BREAKOUT DOWN")
            self.assertTrue(watch.watch_allowed)
            self.assertEqual(watch.direction, "BEARISH")
            self.assertEqual(watch.trigger_level, 100.7)
            self.assertIn("failed opening-range breakout reversal", watch.notes)

        def test_bullish_break_does_not_text_when_fast_move_is_bearish(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["TEST"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_opposed_fast_move.csv", LOG_DIR / "test_opposed_fast_move.jsonl"),
                StateStore(STATE_DIR / "test_opposed_fast_move_state.json"),
            )
            now = now_utc()
            bars = [
                Bar(t=now - timedelta(minutes=12 - i), o=100.0, h=101.0, l=99.8, c=100.0 + i * 0.08, v=20000)
                for i in range(11)
            ]
            bars.append(Bar(t=now, o=101.0, h=101.2, l=100.6, c=100.85, v=36000))
            snap = SymbolSnapshot(
                symbol="TEST",
                latest_bar=bars[-1],
                recent_bars=bars,
                premarket_high=100.2,
                opening_range_high=100.4,
                opening_range_low=99.0,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alert = Alert(
                symbol="TEST",
                timestamp=now,
                category="PREMARKET HIGH BREAK",
                price=100.85,
                trigger_level=100.2,
                setup_level="ALERT",
                fast_move_pct=-0.16,
                day_move_pct=0.25,
                relative_volume=2.0,
            )
            graded = scanner.grade_alert(scanner.attach_option_context(alert, snap), snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertEqual(graded.direction, "BULLISH")
            self.assertFalse(graded.sms_allowed)
            self.assertIn("latest fast move opposes setup direction", graded.text_alert_reason)

        def test_sustained_index_trend_creates_bullish_watch_before_full_rvol(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["SPY"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_sustained_trend_watch.csv", LOG_DIR / "test_sustained_trend_watch.jsonl"),
                StateStore(STATE_DIR / "test_sustained_trend_watch_state.json"),
            )
            start = now_utc() - timedelta(minutes=13)
            closes = [757.72, 757.78, 757.83, 757.88, 757.94, 758.02, 758.08, 758.16, 758.25, 758.34, 758.43, 758.55, 758.64]
            bars = [
                Bar(t=start + timedelta(minutes=i), o=close - 0.03, h=close + 0.05, l=close - 0.06, c=close, v=20000)
                for i, close in enumerate(closes)
            ]
            snap = SymbolSnapshot(
                symbol="SPY",
                latest_bar=bars[-1],
                recent_bars=bars,
                premarket_high=757.37,
                opening_range_high=757.71,
                opening_range_low=756.91,
                best_call=OptionSelection(self.option_contract("C", bid=1.95, ask=2.02), "Tradable", 90),
            )
            alerts = scanner.evaluate_symbol(snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            watch = next(alert for alert in alerts if alert.category == "WATCH SUSTAINED TREND UP")
            self.assertTrue(watch.watch_allowed)
            self.assertFalse(watch.sms_allowed)
            self.assertEqual(watch.direction, "BULLISH")
            self.assertIn("sustained 12m trend move", " | ".join(watch.notes))

        def test_fast_impulse_watch_detects_dramatic_regular_hours_jump(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["AAPL"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_fast_impulse_watch.csv", LOG_DIR / "test_fast_impulse_watch.jsonl"),
                StateStore(STATE_DIR / "test_fast_impulse_watch_state.json"),
            )
            start = set_today_time_et(config["market_open"]).astimezone(UTC) + timedelta(minutes=40)
            closes = [314.05, 314.04, 314.02, 314.03, 314.01, 314.00, 314.15, 314.44, 314.78, 315.12]
            bars = [
                Bar(t=start + timedelta(minutes=i), o=close - 0.08, h=close + 0.10, l=close - 0.12, c=close, v=25000)
                for i, close in enumerate(closes)
            ]
            snap = SymbolSnapshot(symbol="AAPL", latest_bar=bars[-1], recent_bars=bars)
            watch = scanner.maybe_fast_impulse_watch(
                snap,
                bars[-1],
                fast_move=pct_change(bars[-1].c, bars[-6].c),
                day_move=1.2,
                rel_vol=2.2,
                notes=["data quality: Fresh"],
            )
            self.assertIsNotNone(watch)
            assert watch is not None
            self.assertEqual(watch.category, "WATCH FAST IMPULSE UP")
            self.assertEqual(watch.setup_level, "WATCH")
            self.assertIn("fast 3m impulse move", " | ".join(watch.notes))

        def test_fast_impulse_watch_gets_clean_watch_reason(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["AAPL"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_fast_impulse_reason.csv", LOG_DIR / "test_fast_impulse_reason.jsonl"),
                StateStore(STATE_DIR / "test_fast_impulse_reason_state.json"),
            )
            bars = [
                Bar(t=now_utc() - timedelta(minutes=9 - i), o=100.0 + i * 0.1, h=100.3 + i * 0.1, l=99.9 + i * 0.1, c=100.1 + i * 0.1, v=25000)
                for i in range(10)
            ]
            snap = SymbolSnapshot(
                symbol="AAPL",
                latest_bar=bars[-1],
                recent_bars=bars,
                best_call=OptionSelection(self.option_contract("C", bid=1.00, ask=1.04), "Tradable", 82),
            )
            alert = Alert(
                symbol="AAPL",
                timestamp=now_utc(),
                category="WATCH FAST IMPULSE UP",
                price=101.0,
                fast_move_pct=0.35,
                day_move_pct=1.0,
                relative_volume=2.2,
                option_quality="Tradable",
                options_score=82,
                option_spread_pct=3.9,
                setup_level="WATCH",
            )
            graded = scanner.grade_alert(alert, snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertTrue(graded.watch_allowed)
            self.assertFalse(graded.sms_allowed)
            self.assertIn("fast impulse", graded.text_alert_reason)

        def test_generic_high_rvol_does_not_text_when_day_trend_conflicts(self) -> None:
            config = load_config(None)
            scanner = EliteScanner(
                config,
                MockProvider(["NVDA"]),
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_generic_day_conflict.csv", LOG_DIR / "test_generic_day_conflict.jsonl"),
                StateStore(STATE_DIR / "test_generic_day_conflict_state.json"),
            )
            bars = [
                Bar(t=now_utc() - timedelta(minutes=9 - i), o=100.0, h=100.4, l=99.8, c=100.1, v=25000)
                for i in range(10)
            ]
            snap = SymbolSnapshot(
                symbol="NVDA",
                latest_bar=bars[-1],
                recent_bars=bars,
                best_call=OptionSelection(self.option_contract("C", bid=1.00, ask=1.03), "Tradable", 82),
            )
            alert = Alert(
                symbol="NVDA",
                timestamp=now_utc(),
                category="HIGH RELATIVE VOLUME",
                price=222.68,
                fast_move_pct=0.16,
                day_move_pct=-1.99,
                relative_volume=3.8,
                option_quality="Tradable",
                options_score=82,
                option_spread_pct=2.9,
            )
            graded = scanner.grade_alert(alert, snap, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            self.assertEqual(graded.direction, "BULLISH")
            self.assertFalse(graded.sms_allowed)
            self.assertIn("day trend conflicts with small fast move", graded.text_alert_reason)

        def test_compact_alert_message_is_short_and_actionable(self) -> None:
            alert = Alert(
                symbol="NVDA",
                timestamp=now_utc(),
                category="OPENING RANGE BREAK DOWN",
                price=212.25,
                fast_move_pct=-0.42,
                day_move_pct=-1.2,
                relative_volume=2.4,
                option_quality="Tradable",
                option_spread_pct=3.1,
                direction="BEARISH",
                alert_grade="A",
                setup_level="ALERT",
                trigger_level=212.50,
            )
            message = compact_alert_message(alert)
            self.assertIn("ALERT NVDA BEARISH", message)
            self.assertIn("level 212.50", message)
            self.assertIn("confirm in Webull", message)
            self.assertLess(len(message), 180)

        def test_messages_phone_list_supports_multiple_recipients(self) -> None:
            numbers = parse_phone_numbers("2125550101, (917) 555-0102; 5551234567")
            self.assertEqual(numbers, ["2125550101", "(917) 555-0102", "5551234567"])

        def test_messages_phone_numbers_are_normalized(self) -> None:
            self.assertEqual(normalize_phone_for_messages("2125550101"), "+12125550101")
            self.assertEqual(normalize_phone_for_messages("(917) 555-0102"), "+19175550102")
            self.assertEqual(normalize_phone_for_messages("+19175550102"), "+19175550102")

        def test_dry_run_options_are_simulated_and_labeled(self) -> None:
            config = load_config(None)
            provider = MockProvider(["ASTS"])
            chain = provider.get_option_chain("ASTS", config)
            self.assertTrue(chain)
            self.assertTrue(all(contract.is_simulated for contract in chain))
            selection = choose_best_option_contract(chain, "C", provider._base_price("ASTS"), config)
            alert = Alert(symbol="ASTS", timestamp=now_utc(), category="OPENING RANGE BREAK UP", price=100, fast_move_pct=1.0)
            scanner = EliteScanner(
                config,
                provider,
                DiscordNotifier(None),
                AlertWriter(LOG_DIR / "test_sim_options_alerts.csv", LOG_DIR / "test_sim_options_alerts.jsonl"),
                StateStore(STATE_DIR / "test_sim_options_state.json"),
            )
            snap = SymbolSnapshot(symbol="ASTS", best_call=selection)
            enriched = scanner.attach_option_context(alert, snap)
            self.assertTrue(any("simulated dry-run" in note for note in enriched.notes))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ScannerTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def run_phone_push_test() -> int:
    token = (os.getenv("PUSHOVER_APP_TOKEN") or "").strip()
    user_key = (os.getenv("PUSHOVER_USER_KEY") or "").strip()
    if not token or not user_key:
        logger.error("Phone push is not configured. Add PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY to .env.")
        return 2

    payload = {
        "token": token,
        "user": user_key,
        "title": "Elite Scanner Phone Push Test",
        "message": "Phone notifications are connected. Future A/A+ scanner alerts can reach this phone. Confirm trades in Webull.",
        "priority": 1,
        "sound": "cashregister",
    }
    try:
        resp = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=20)
    except Exception as exc:
        logger.error("Pushover test failed before the server replied: %s", exc)
        return 1
    if resp.status_code != 200:
        logger.error("Pushover test failed: HTTP %s %s", resp.status_code, resp.text[:500])
        return 1
    logger.info("Pushover phone push test sent successfully.")
    return 0


def run_desktop_notification_test() -> int:
    script = """
    display notification "Computer alerts are connected. Future high-quality scanner alerts will pop up here. Confirm trades in Webull." with title "Elite Scanner Test" subtitle "Desktop notifications" sound name "Glass"
    """
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=10)
    except Exception as exc:
        logger.error("Mac desktop notification test failed: %s", exc)
        return 1
    logger.info("Mac desktop notification test sent successfully.")
    return 0


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def make_provider(mode: str, symbols: List[str], config: Optional[Dict[str, Any]] = None) -> DataProvider:
    if mode in {"dry-run", "test"}:
        return MockProvider(symbols)
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY for live mode.")
    return AlpacaProvider(api_key, secret_key, feed=stock_feed_from_config(config or DEFAULT_CONFIG))


def log_market_data_startup_status(provider: DataProvider, config: Dict[str, Any], symbol: str = "AAPL") -> Dict[str, Any]:
    if isinstance(provider, AlpacaProvider):
        status = provider.check_market_data_status(config, symbol=symbol)
    else:
        checked_at = now_utc().isoformat()
        status = {
            "timestamp": checked_at,
            "last_data_check_time": checked_at,
            "symbol": symbol,
            "stock_feed_requested": "SIMULATED",
            "stock_feed_status": "simulated",
            "options_feed_requested": "SIMULATED",
            "options_feed_status": "simulated",
            "opra_status": "not_applicable",
            "api_rate_limit_mode": "dry-run/test",
            "websocket_symbol_limit": "dry-run/test",
            "allow_indicative_options_fallback": bool(config.get("options", {}).get("allow_indicative_fallback", True)),
            "feed_warning": "",
        }
    append_market_data_status(status)
    logger.info("Market data status | Stock feed requested: %s", status.get("stock_feed_requested", "UNKNOWN"))
    logger.info("Market data status | Options feed requested: %s", status.get("options_feed_requested", "UNKNOWN"))
    logger.info("Market data status | OPRA status: %s", status.get("opra_status", "unknown"))
    logger.info("Market data status | Options feed status: %s", status.get("options_feed_status", "unknown"))
    logger.info("Market data status | API rate limit mode: %s", status.get("api_rate_limit_mode", "unknown"))
    logger.info("Market data status | Websocket symbol limit: %s", status.get("websocket_symbol_limit", "unknown"))
    if status.get("feed_warning"):
        logger.warning("Market data status | %s", status["feed_warning"])
    return status


def log_scanner_startup_status(config: Dict[str, Any], market_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = {
        "timestamp": now_utc().isoformat(),
        **scanner_identity(config),
        "stock_feed": (market_status or {}).get("stock_feed_status")
        or str(config.get("market_data", {}).get("stock_feed", "unknown")).upper(),
        "options_feed": (market_status or {}).get("options_feed_status")
        or str(config.get("options", {}).get("feed", "unknown")).upper(),
        "opra_status": (market_status or {}).get("opra_status", "unknown"),
    }
    try:
        with SCANNER_STARTUP_STATUS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception as exc:
        logger.warning("Scanner startup status log failed: %s", redact_notification_error(exc))
    logger.info(
        "Scanner identity | instance=%s role=%s profile=%s host=%s commit=%s branch=%s",
        payload["scanner_instance_name"],
        payload["scanner_machine_role"],
        payload["scanner_alert_profile"],
        payload["hostname"],
        payload["git_commit"],
        payload["git_branch"],
    )
    logger.info(
        "Scanner identity | alerts=%s context=%s Telegram=%s/*%s",
        ",".join(payload["alert_symbols"]),
        ",".join(payload["context_symbols"]),
        payload["telegram_destination_type"],
        payload["telegram_chat_id_last4"],
    )
    return payload


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Elite Momentum Scanner")
    parser.add_argument("--mode", choices=["live", "dry-run", "test"], default="dry-run")
    parser.add_argument("--config", type=str, help="Optional JSON config path")
    parser.add_argument("--test-phone-push", action="store_true", help="Send a test Pushover phone notification")
    parser.add_argument("--test-desktop-notification", action="store_true", help="Send a test Mac desktop notification")
    args = parser.parse_args()

    config_path = Path(args.config).resolve() if args.config else None
    config = load_config(config_path)

    if args.test_phone_push:
        return run_phone_push_test()

    if args.test_desktop_notification:
        return run_desktop_notification_test()

    if args.mode == "test":
        return run_tests()

    provider = make_provider(args.mode, list(config["symbols"]), config)
    notifier = make_notifier(config)
    writer = AlertWriter(Path(config["outputs"]["csv_log"]), Path(config["outputs"]["jsonl_log"]))
    state = StateStore(Path(config["outputs"]["state_file"]))
    scanner = EliteScanner(config, provider, notifier, writer, state)

    if args.mode == "dry-run":
        logger.info("Running dry-run scanner. Press Ctrl+C to stop.")
    else:
        logger.info("Running live scanner. Press Ctrl+C to stop.")
    if config.get("notifications", {}).get("telegram_send_test_on_start", False):
        send_telegram_test_message(config)
    market_status = log_market_data_startup_status(provider, config, symbol=(config.get("symbols") or ["AAPL"])[0])
    log_scanner_startup_status(config, market_status)
    scanner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
