from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..base import ema, pct_change, vwap


def _resample(bars: List[Any], minutes: int) -> List[Dict[str, Any]]:
    buckets: Dict[datetime, List[Any]] = {}
    for bar in bars:
        timestamp = bar.t.replace(second=0, microsecond=0)
        bucket = timestamp.replace(minute=(timestamp.minute // minutes) * minutes)
        buckets.setdefault(bucket, []).append(bar)
    return [
        {
            "t": timestamp,
            "o": group[0].o,
            "h": max(bar.h for bar in group),
            "l": min(bar.l for bar in group),
            "c": group[-1].c,
            "v": sum(bar.v for bar in group),
        }
        for timestamp, group in sorted(buckets.items())
    ]


def _trend(bars: List[Any]) -> str:
    if len(bars) < 3:
        return "UNKNOWN"
    closes = [bar["c"] if isinstance(bar, dict) else bar.c for bar in bars]
    current_ema9 = ema(closes, 9)
    prior_ema9 = ema(closes[:-1], 9)
    current_ema20 = ema(closes, 20)
    prior_ema20 = ema(closes[:-1], 20)
    if not all(value is not None for value in (current_ema9, prior_ema9, current_ema20, prior_ema20)):
        return "UNKNOWN"
    latest = closes[-1]
    if latest > current_ema9 > current_ema20 and current_ema9 > prior_ema9 and current_ema20 >= prior_ema20:
        return "BULLISH"
    if latest < current_ema9 < current_ema20 and current_ema9 < prior_ema9 and current_ema20 <= prior_ema20:
        return "BEARISH"
    return "MIXED"


def _valid_levels(levels: Dict[str, Optional[float]]) -> Dict[str, float]:
    return {
        name: float(value)
        for name, value in levels.items()
        if isinstance(value, (int, float)) and value > 0
    }


def _zones(bars: List[Any], price: float) -> tuple[List[Dict[str, float]], List[Dict[str, float]]]:
    recent = bars[-20:]
    demand: List[Dict[str, float]] = []
    supply: List[Dict[str, float]] = []
    for index in range(1, len(recent) - 1):
        bar = recent[index]
        if bar.l <= recent[index - 1].l and bar.l <= recent[index + 1].l and bar.l <= price:
            demand.append({"low": round(bar.l, 4), "high": round(max(bar.o, bar.c), 4)})
        if bar.h >= recent[index - 1].h and bar.h >= recent[index + 1].h and bar.h >= price:
            supply.append({"low": round(min(bar.o, bar.c), 4), "high": round(bar.h, 4)})
    return demand[-3:], supply[-3:]


def evaluate_multi_timeframe_context(
    bars: List[Any],
    *,
    daily_bars: Optional[List[Any]] = None,
    premarket_high: Optional[float] = None,
    premarket_low: Optional[float] = None,
) -> Dict[str, Any]:
    if not bars:
        return {
            "trend_1m": "UNKNOWN",
            "trend_5m": "UNKNOWN",
            "trend_15m": "UNKNOWN",
            "daily_trend": "UNKNOWN",
            "current_bias": "UNKNOWN",
            "key_warning": "Intraday bars unavailable",
            "levels": {},
            "demand_zones": [],
            "supply_zones": [],
            "liquidity_above_highs": [],
            "liquidity_below_lows": [],
        }

    latest = bars[-1]
    daily_bars = daily_bars or []
    previous_day = daily_bars[-1] if daily_bars else None
    session_high = max(bar.h for bar in bars)
    session_low = min(bar.l for bar in bars)
    current_vwap = vwap(bars)
    closes = [bar.c for bar in bars]
    current_ema9 = ema(closes, 9)
    current_ema20 = ema(closes, 20)
    levels = _valid_levels(
        {
            "pmh": premarket_high,
            "pml": premarket_low,
            "pdh": previous_day.h if previous_day else None,
            "pdl": previous_day.l if previous_day else None,
            "pdc": previous_day.c if previous_day else None,
            "hod": session_high,
            "lod": session_low,
            "vwap": current_vwap,
            "ema9": current_ema9,
            "ema20": current_ema20,
        }
    )
    support = sorted(
        ((name, level) for name, level in levels.items() if level <= latest.c),
        key=lambda item: latest.c - item[1],
    )
    resistance = sorted(
        ((name, level) for name, level in levels.items() if level >= latest.c),
        key=lambda item: item[1] - latest.c,
    )
    nearest_support = support[0] if support else None
    nearest_resistance = resistance[0] if resistance else None
    candidates = [item for item in (nearest_support, nearest_resistance) if item]
    nearest_level = min(candidates, key=lambda item: abs(latest.c - item[1])) if candidates else None
    demand_zones, supply_zones = _zones(bars, latest.c)
    trend_1m = _trend(bars[-30:])
    trend_5m = _trend(_resample(bars, 5)[-20:])
    trend_15m = _trend(_resample(bars, 15)[-20:])
    daily_trend = _trend(daily_bars[-20:])
    directional = [trend for trend in (trend_1m, trend_5m, trend_15m) if trend in {"BULLISH", "BEARISH"}]
    if directional and len(set(directional)) == 1 and len(directional) >= 2:
        current_bias = directional[0]
        key_warning = ""
    elif trend_5m in {"BULLISH", "BEARISH"} and trend_15m in {"BULLISH", "BEARISH"} and trend_5m != trend_15m:
        current_bias = "CONFLICTED"
        key_warning = "1m/5m setup disagrees with 15m structure"
    else:
        current_bias = "MIXED"
        key_warning = "Multi-timeframe structure is not aligned"

    return {
        "trend_1m": trend_1m,
        "trend_5m": trend_5m,
        "trend_15m": trend_15m,
        "daily_trend": daily_trend,
        "current_bias": current_bias,
        "key_warning": key_warning,
        "nearest_level_name": nearest_level[0].upper() if nearest_level else None,
        "nearest_level_price": round(nearest_level[1], 4) if nearest_level else None,
        "distance_to_key_level_pct": round(abs(pct_change(latest.c, nearest_level[1])), 4) if nearest_level else None,
        "nearest_support_name": nearest_support[0].upper() if nearest_support else None,
        "nearest_support": round(nearest_support[1], 4) if nearest_support else None,
        "nearest_resistance_name": nearest_resistance[0].upper() if nearest_resistance else None,
        "nearest_resistance": round(nearest_resistance[1], 4) if nearest_resistance else None,
        "levels": levels,
        "demand_zones": demand_zones,
        "supply_zones": supply_zones,
        "liquidity_above_highs": [
            {"name": name.upper(), "price": round(level, 4)}
            for name, level in resistance
            if name in {"pmh", "pdh", "hod"}
        ],
        "liquidity_below_lows": [
            {"name": name.upper(), "price": round(level, 4)}
            for name, level in support
            if name in {"pml", "pdl", "lod"}
        ],
    }
