#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner
from scanner import combine_market_structure, detect_supply_demand, detect_support_resistance
from scanner.market_structure_models import ENGINE_VERSION, resample_bars
from scanner.market_structure_models import normalize_bars
from strategies.base import ema


LOG_PATHS = {
    "support_resistance": ROOT / "logs" / "support_resistance_levels.jsonl",
    "supply_demand": ROOT / "logs" / "supply_demand_zones.jsonl",
    "summary": ROOT / "logs" / "market_structure.jsonl",
}
ET = ZoneInfo("America/New_York")


def _known_levels(bars: List[Any], daily_bars: List[Any], config: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_bars(bars)
    normalized_daily = normalize_bars(daily_bars)
    closes = [bar["c"] for bar in normalized]
    volume = sum(bar["v"] for bar in normalized)
    current_vwap = (
        sum(((bar["h"] + bar["l"] + bar["c"]) / 3.0) * bar["v"] for bar in normalized) / volume
        if volume > 0
        else None
    )
    latest_day = normalized_daily[-1] if normalized_daily else None
    if latest_day and normalized and hasattr(latest_day["t"], "astimezone") and hasattr(normalized[-1]["t"], "astimezone"):
        if latest_day["t"].astimezone(ET).date() == normalized[-1]["t"].astimezone(ET).date() and len(normalized_daily) > 1:
            latest_day = normalized_daily[-2]
    levels: Dict[str, Any] = {
        "vwap": current_vwap,
        "ema9": ema(closes, 9),
        "ema20": ema(closes, 20),
        "hod": max((bar["h"] for bar in normalized), default=None),
        "lod": min((bar["l"] for bar in normalized), default=None),
        "pdh": latest_day["h"] if latest_day else None,
        "pdl": latest_day["l"] if latest_day else None,
        "pdc": latest_day["c"] if latest_day else None,
    }
    hour, minute = [int(part) for part in config.get("market_open", "09:30").split(":", 1)]
    range_minutes = int(config.get("opening_range_minutes", 5))
    opening_bars = []
    for bar in normalized:
        if not hasattr(bar["t"], "astimezone"):
            continue
        local = bar["t"].astimezone(ET)
        start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start <= local < start + timedelta(minutes=range_minutes):
            opening_bars.append(bar)
    levels["opening_range_high"] = max((bar["h"] for bar in opening_bars), default=None)
    levels["opening_range_low"] = min((bar["l"] for bar in opening_bars), default=None)
    premarket = [
        bar
        for bar in normalized
        if hasattr(bar["t"], "astimezone")
        and (bar["t"].astimezone(ET).hour, bar["t"].astimezone(ET).minute) < (hour, minute)
        and bar["t"].astimezone(ET).date() == normalized[-1]["t"].astimezone(ET).date()
    ]
    levels["pmh"] = max((bar["h"] for bar in premarket), default=None)
    levels["pml"] = min((bar["l"] for bar in premarket), default=None)
    return levels


def build_market_structure(
    symbol: str,
    bars: Iterable[Any],
    *,
    daily_bars: Iterable[Any] = (),
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config = config or scanner.load_config(None)
    structure_config = config.get("market_structure_engines", {})
    minute_bars = list(bars)
    daily = list(daily_bars)
    known_levels = _known_levels(minute_bars, daily, config)
    current_price = float(minute_bars[-1].c if hasattr(minute_bars[-1], "c") else minute_bars[-1]["c"]) if minute_bars else None
    frame_bars = {
        "1m": resample_bars(minute_bars, 1),
        "5m": resample_bars(minute_bars, 5),
        "15m": resample_bars(minute_bars, 15),
    }
    support_resistance: Dict[str, Dict[str, Any]] = {}
    supply_demand: Dict[str, Dict[str, Any]] = {}
    for timeframe, candles in frame_bars.items():
        support_resistance[timeframe] = detect_support_resistance(
            symbol,
            timeframe,
            candles,
            current_price=current_price,
            known_levels=known_levels,
            max_levels=int(structure_config.get("max_levels_per_timeframe", 3)),
            min_strength=int(structure_config.get("min_level_strength", 55)),
        )
        supply_demand[timeframe] = detect_supply_demand(
            symbol,
            timeframe,
            candles,
            current_price=current_price,
            known_levels=known_levels,
            support_resistance=support_resistance[timeframe],
            max_zones=int(structure_config.get("max_zones_per_timeframe", 3)),
            min_strength=int(structure_config.get("min_zone_strength", 55)),
        )
    return {
        "symbol": symbol,
        "timestamp": scanner.now_utc().isoformat(),
        "engine_version": ENGINE_VERSION,
        "support_resistance": support_resistance,
        "supply_demand": supply_demand,
        "summary": combine_market_structure(symbol, support_resistance, supply_demand),
        "context_only": True,
        "can_approve_trades": False,
    }


def _append_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")


def write_logs(payload: Dict[str, Any], config: Dict[str, Any]) -> None:
    identity = scanner.scanner_identity(config)
    timestamp = payload["timestamp"]
    common = {
        "timestamp": timestamp,
        "symbol": payload["symbol"],
        "git_commit": identity.get("git_commit"),
        "engine_version": ENGINE_VERSION,
        "context_only": True,
    }
    _append_jsonl(
        LOG_PATHS["support_resistance"],
        (
            {
                **common,
                "timeframe": timeframe,
                "current_price": result.get("current_price"),
                "levels": {
                    "support": result.get("support_levels", []),
                    "resistance": result.get("resistance_levels", []),
                },
                "nearest_levels": {
                    "support": result.get("nearest_support_below", {}),
                    "resistance": result.get("nearest_resistance_above", {}),
                },
                "current_price_location": result.get("current_price_location"),
                "errors": result.get("reason"),
            }
            for timeframe, result in payload["support_resistance"].items()
        ),
    )
    _append_jsonl(
        LOG_PATHS["supply_demand"],
        (
            {
                **common,
                "timeframe": timeframe,
                "current_price": result.get("current_price"),
                "zones": {
                    "demand": result.get("demand_zones", []),
                    "supply": result.get("supply_zones", []),
                },
                "nearest_zones": {
                    "demand": result.get("nearest_demand_below", {}),
                    "supply": result.get("nearest_supply_above", {}),
                },
                "current_price_location": result.get("current_price_location"),
                "errors": result.get("reason"),
            }
            for timeframe, result in payload["supply_demand"].items()
        ),
    )
    _append_jsonl(LOG_PATHS["summary"], ({**common, **payload["summary"]},))


def render_pretty(payload: Dict[str, Any]) -> str:
    lines = [f"{payload['symbol']} Live Market Structure", "", f"Current price: {payload['summary'].get('current_price') or 'unavailable'}"]
    for timeframe in ("1m", "5m", "15m"):
        result = payload["support_resistance"][timeframe]
        for label, key in (("Support", "support_levels"), ("Resistance", "resistance_levels")):
            lines.extend(["", f"{timeframe} {label}:"])
            levels = result.get(key, [])
            if not levels:
                lines.append("No clean levels detected.")
            for index, level in enumerate(levels, 1):
                lines.append(
                    f"{index}) {level['price']:.2f} | {level['strength']} | tested {level['times_tested']}x | {level['source']}"
                )
        zones = payload["supply_demand"][timeframe]
        for label, key in (("Demand", "demand_zones"), ("Supply", "supply_zones")):
            lines.extend(["", f"{timeframe} {label}:"])
            items = zones.get(key, [])
            if not items:
                lines.append("No clean zones detected.")
            for index, zone in enumerate(items, 1):
                freshness = "fresh" if zone["fresh"] else f"tested {zone['times_tested']}x"
                lines.append(
                    f"{index}) {zone['zone_low']:.2f}-{zone['zone_high']:.2f} | {zone['strength']} | "
                    f"{freshness} | {zone['last_reaction']}"
                )
    summary = payload["summary"]
    lines.extend(
        [
            "",
            "Summary:",
            summary["current_price_location_summary"] + ".",
            f"Structure: {summary['market_structure_bias']}.",
            f"Warning: {summary['structure_warning']}.",
            "",
            "Read-only market-structure context. No alerts, OpenAI calls, Telegram messages, or orders were generated.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview read-only AAPL support/resistance and supply/demand context.")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--minutes", type=int, default=240)
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
    provider = scanner.make_provider("live", [symbol], config)
    end = scanner.now_utc()
    bars = provider.get_recent_bars([symbol], end - timedelta(minutes=max(30, args.minutes)), end).get(symbol, [])
    daily = provider.get_daily_bars([symbol], end - timedelta(days=10), end).get(symbol, [])
    payload = build_market_structure(symbol, bars, daily_bars=daily, config=config)
    if not args.no_log:
        write_logs(payload, config)
    print(json.dumps(payload, indent=2, default=str, sort_keys=True) if args.json else render_pretty(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
