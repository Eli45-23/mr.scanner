#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner
from scanner.liquidity_sweep_engine import ENGINE_VERSION, evaluate_liquidity_sweeps
from scanner.market_structure_models import normalize_bars
from tools.preview_market_structure import _known_levels, build_market_structure

LOG_PATH = ROOT / "logs" / "liquidity_sweeps.jsonl"


def build_liquidity_sweep_preview(
    symbol: str,
    bars: Iterable[Any],
    *,
    daily_bars: Iterable[Any] = (),
    config: Dict[str, Any] | None = None,
    current_candle_closed: bool | None = None,
    market_structure: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config = config or scanner.load_config(None)
    minute_bars, daily = list(bars), list(daily_bars)
    structure = market_structure or build_market_structure(symbol, minute_bars, daily_bars=daily, config=config)
    settings = config.get("liquidity_sweep_engine", {})
    normalized = normalize_bars(minute_bars)
    if current_candle_closed is None:
        latest_time = normalized[-1]["t"] if normalized else None
        current_candle_closed = bool(
            latest_time is not None
            and hasattr(latest_time, "tzinfo")
            and latest_time + timedelta(minutes=1) <= scanner.now_utc()
        )
    known_levels = _known_levels(minute_bars, daily, config)
    if normalized:
        prior = normalized[-21:-1] or normalized[:-1]
        known_levels["recent_swing_high"] = max((bar["h"] for bar in prior), default=None)
        known_levels["recent_swing_low"] = min((bar["l"] for bar in prior), default=None)
    result = evaluate_liquidity_sweeps(
        symbol,
        minute_bars,
        market_structure=structure,
        known_levels=known_levels,
        current_candle_closed=current_candle_closed,
        watch_distance_bps=float(settings.get("watch_distance_bps", 8)),
        min_confidence_score=int(settings.get("min_confidence", 55)),
        timeframes=settings.get("timeframes", ["1m", "5m", "15m"]),
        use_supply_demand=bool(settings.get("use_supply_demand", True)),
        use_support_resistance=bool(settings.get("use_support_resistance", True)),
    )
    summary = structure.get("summary") if isinstance(structure.get("summary"), dict) else {}
    result["market_structure_summary"] = summary.get("current_price_location_summary")
    return result


def write_log(payload: Dict[str, Any], config: Dict[str, Any], path: Path = LOG_PATH) -> None:
    record = {
        key: payload.get(key)
        for key in (
            "timestamp", "symbol", "current_price", "sweep_status", "sweep_direction", "trap_bias", "sweep_level",
            "sweep_zone_low", "sweep_zone_high", "level_source", "timeframe", "confidence", "score",
            "reason", "meaning", "wait_for", "invalidation", "current_candle_closed",
            "inside_chop_range", "related_demand_zone", "related_supply_zone",
            "nearest_upside_sweep_zone", "nearest_downside_sweep_zone",
            "sweep_map_status", "sweep_event_status", "map_only", "event_alert_candidate",
            "possible_sweep_zones", "zone_bucket", "alert_state", "repeated_range_sweeps",
            "telegram_filter_allowed", "telegram_filter_reason", "suppression_type", "dashboard_only_reason",
        )
    }
    record.update({
        "git_commit": scanner.scanner_identity(config).get("git_commit"),
        "engine_version": ENGINE_VERSION,
        "context_only": True,
        "can_approve_trades": False,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")


def _zone_text(candidate: Any) -> str:
    if not isinstance(candidate, dict):
        return "Not enough clean data yet"
    low, high, level = candidate.get("zone_low"), candidate.get("zone_high"), candidate.get("level")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        price = f"{low:.2f}-{high:.2f}"
    elif isinstance(level, (int, float)):
        price = f"{level:.2f}"
    else:
        return "Not enough clean data yet"
    return f"{price} | {candidate.get('source', 'unknown')} | {candidate.get('timeframe', 'unknown')}"


def render_pretty(payload: Dict[str, Any]) -> str:
    return "\n".join([
        f"{payload['symbol']} Liquidity Sweep Preview",
        "",
        f"Current price: {payload.get('current_price') or 'unavailable'}",
        f"Nearest upside sweep zone: {_zone_text(payload.get('nearest_upside_sweep_zone'))}",
        f"Nearest downside sweep zone: {_zone_text(payload.get('nearest_downside_sweep_zone'))}",
        "",
        f"Current sweep status: {payload['sweep_status']}",
        f"Sweep direction: {payload['sweep_direction']}",
        f"Trap bias: {payload['trap_bias']}",
        f"Confidence: {payload['score']} {payload['confidence']}",
        f"Reason: {payload['reason']}",
        f"Wait for: {payload['wait_for']}",
        f"Invalidation: {payload['invalidation']}",
        "",
        "Read-only liquidity-sweep context. No alerts, Telegram messages, or orders were generated.",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview read-only AAPL liquidity sweep context.")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--minutes", type=int, default=390)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args()
    symbol = args.symbol.upper()
    scanner.load_dotenv()
    config = scanner.load_config(None)
    if symbol not in config.get("symbols", ["AAPL"]):
        print(f"{symbol} is not an official alert symbol. This preview is restricted to AAPL.", file=sys.stderr)
        return 2
    try:
        provider = scanner.make_provider("live", [symbol], config)
    except RuntimeError as exc:
        print(f"Liquidity sweep preview unavailable: {exc}", file=sys.stderr)
        return 1
    end = scanner.now_utc()
    bars = provider.get_recent_bars([symbol], end - timedelta(minutes=max(30, args.minutes)), end).get(symbol, [])
    daily = provider.get_daily_bars([symbol], end - timedelta(days=10), end).get(symbol, [])
    payload = build_liquidity_sweep_preview(symbol, bars, daily_bars=daily, config=config)
    if not args.no_log:
        write_log(payload, config)
    print(json.dumps(payload, indent=2, default=str, sort_keys=True) if args.json else render_pretty(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
