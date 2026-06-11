from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


@dataclass
class StrategyContext:
    symbol: str
    bars: List[Any]
    latest: Any
    config: Dict[str, Any]
    levels: Dict[str, Optional[float]]
    relative_volume: Optional[float] = None
    market_alignment: str = "UNKNOWN"
    liquidity_sweep_context: Optional[Dict[str, Any]] = None


@dataclass
class StrategyResult:
    strategy: str
    label: str
    direction: str = "neutral"
    active: bool = False
    score: int = 0
    confidence_label: str = "LOW"
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    levels: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "label": self.label,
            "direction": self.direction,
            "active": self.active,
            "score": self.score,
            "confidence_label": self.confidence_label,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "levels": self.levels,
        }


def confidence_label(score: int) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 60:
        return "MEDIUM"
    return "LOW"


def clamp_score(score: float) -> int:
    return int(max(0, min(100, round(score))))


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100.0


def average(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def ema(values: List[float], period: int = 9) -> Optional[float]:
    if not values:
        return None
    k = 2 / (period + 1)
    current = values[0]
    for value in values[1:]:
        current = value * k + current * (1 - k)
    return current


def vwap(bars: List[Any]) -> Optional[float]:
    pv = 0.0
    vol = 0.0
    for bar in bars:
        typical = (bar.h + bar.l + bar.c) / 3.0
        pv += typical * bar.v
        vol += bar.v
    if vol <= 0:
        return None
    return pv / vol


def recent_volume_multiplier(bars: List[Any], lookback: int = 10) -> Optional[float]:
    if len(bars) < 2:
        return None
    prior = bars[-(lookback + 1):-1] if len(bars) > lookback else bars[:-1]
    avg = average([bar.v for bar in prior])
    if not avg or avg <= 0:
        return None
    return bars[-1].v / avg


def is_volume_confirmed(ctx: StrategyContext) -> bool:
    minimum = float(ctx.config.get("strategy_engine", {}).get("volume_confirm_multiplier", 1.5))
    vol_mult = ctx.relative_volume or recent_volume_multiplier(ctx.bars) or 0.0
    return vol_mult >= minimum


def market_supports(ctx: StrategyContext, direction: str) -> bool:
    if ctx.market_alignment in {"ALIGNED", "MIXED", "UNKNOWN"}:
        return True
    return direction == "neutral"


def market_opposes(ctx: StrategyContext, direction: str) -> bool:
    return direction in {"bullish", "bearish"} and ctx.market_alignment == "OPPOSED"


def level_hits(levels: Dict[str, Optional[float]]) -> Dict[str, float]:
    return {k: v for k, v in levels.items() if isinstance(v, (int, float)) and v > 0}


def recent_swing_high(bars: List[Any], lookback: int = 8) -> Optional[float]:
    if len(bars) < 2:
        return None
    window = bars[-(lookback + 1):-1] if len(bars) > lookback else bars[:-1]
    return max((bar.h for bar in window), default=None)


def recent_swing_low(bars: List[Any], lookback: int = 8) -> Optional[float]:
    if len(bars) < 2:
        return None
    window = bars[-(lookback + 1):-1] if len(bars) > lookback else bars[:-1]
    return min((bar.l for bar in window), default=None)


def crossed_above(previous: float, current: float, level: float) -> bool:
    return previous <= level < current


def crossed_below(previous: float, current: float, level: float) -> bool:
    return previous >= level > current


def bars_in_opening_range(bars: List[Any], market_open: str, minutes: int) -> List[Any]:
    if not bars or minutes <= 0:
        return []
    hour, minute = [int(part) for part in market_open.split(":", 1)]
    out = []
    for bar in bars:
        local = bar.t.astimezone()
        start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        end = start + timedelta(minutes=minutes)
        if start <= local < end:
            out.append(bar)
    return out


def opening_range_complete(bars: List[Any], market_open: str, minutes: int) -> bool:
    return len(bars_in_opening_range(bars, market_open, minutes)) >= minutes


def opening_range_levels(bars: List[Any], market_open: str, minutes: int) -> Dict[str, Optional[float]]:
    range_bars = bars_in_opening_range(bars, market_open, minutes)
    if not range_bars:
        return {"high": None, "low": None}
    return {"high": max(bar.h for bar in range_bars), "low": min(bar.l for bar in range_bars)}
