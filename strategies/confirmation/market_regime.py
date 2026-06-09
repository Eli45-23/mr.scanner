from __future__ import annotations

from datetime import timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..base import average, ema, pct_change, recent_volume_multiplier, vwap

ET = ZoneInfo("America/New_York")


def _vwap_cross_count(bars: List[Any], current_vwap: Optional[float]) -> int:
    if not current_vwap or len(bars) < 2:
        return 0
    crosses = 0
    previous_above = bars[0].c > current_vwap
    for bar in bars[1:]:
        above = bar.c > current_vwap
        if above != previous_above:
            crosses += 1
        previous_above = above
    return crosses


def _slope(values: List[float], period: int) -> float:
    if len(values) < 3:
        return 0.0
    current = ema(values, period)
    prior = ema(values[:-1], period)
    if not current or not prior:
        return 0.0
    return pct_change(current, prior)


def _market_state(bars: List[Any], choppy_cross_count: int) -> Dict[str, Any]:
    if len(bars) < 3:
        return {
            "state": "UNKNOWN",
            "direction": "UNKNOWN",
            "score": 0,
            "above_vwap": False,
            "ema9_slope_pct": 0.0,
            "ema20_slope_pct": 0.0,
            "crosses": 0,
            "change_pct": 0.0,
            "distance_from_vwap_pct": 0.0,
        }
    current_vwap = vwap(bars)
    closes = [bar.c for bar in bars]
    ema9_slope = _slope(closes, 9)
    ema20_slope = _slope(closes, 20)
    above_vwap = bool(current_vwap and bars[-1].c > current_vwap)
    below_vwap = bool(current_vwap and bars[-1].c < current_vwap)
    recent = bars[-min(5, len(bars)):]
    higher_highs = sum(recent[i].h > recent[i - 1].h for i in range(1, len(recent)))
    higher_lows = sum(recent[i].l > recent[i - 1].l for i in range(1, len(recent)))
    lower_highs = sum(recent[i].h < recent[i - 1].h for i in range(1, len(recent)))
    lower_lows = sum(recent[i].l < recent[i - 1].l for i in range(1, len(recent)))
    crosses = _vwap_cross_count(bars, current_vwap)
    move = pct_change(bars[-1].c, bars[0].c)

    bull_score = 0
    bear_score = 0
    if above_vwap:
        bull_score += 25
    if below_vwap:
        bear_score += 25
    if ema9_slope > 0:
        bull_score += 15
    if ema9_slope < 0:
        bear_score += 15
    if ema20_slope > 0:
        bull_score += 10
    if ema20_slope < 0:
        bear_score += 10
    if higher_highs >= 3 and higher_lows >= 3:
        bull_score += 25
    if lower_highs >= 3 and lower_lows >= 3:
        bear_score += 25
    if move > 0.15:
        bull_score += 15
    if move < -0.15:
        bear_score += 15

    if crosses >= choppy_cross_count:
        state = "CHOPPY"
        direction = "NEUTRAL"
    elif bull_score > bear_score:
        state = "BULLISH"
        direction = "BULLISH"
    elif bear_score > bull_score:
        state = "BEARISH"
        direction = "BEARISH"
    else:
        state = "FLAT"
        direction = "NEUTRAL"
    return {
        "state": state,
        "direction": direction,
        "score": max(bull_score, bear_score),
        "bull_score": bull_score,
        "bear_score": bear_score,
        "above_vwap": above_vwap,
        "ema9_slope_pct": round(ema9_slope, 4),
        "ema20_slope_pct": round(ema20_slope, 4),
        "crosses": crosses,
        "change_pct": round(move, 4),
        "distance_from_vwap_pct": round(pct_change(bars[-1].c, current_vwap), 4) if current_vwap else 0.0,
        "higher_highs": higher_highs,
        "higher_lows": higher_lows,
        "lower_highs": lower_highs,
        "lower_lows": lower_lows,
    }


def _alignment(aapl_direction: str, market_direction: str) -> str:
    if market_direction == "UNKNOWN":
        return "UNKNOWN"
    if aapl_direction not in {"BULLISH", "BEARISH"} or market_direction == "NEUTRAL":
        return "NEUTRAL"
    return "ALIGNED" if aapl_direction == market_direction else "OPPOSED"


def _volume_state(bars: List[Any]) -> str:
    multiplier = recent_volume_multiplier(bars, lookback=10) or 0.0
    if multiplier >= 3.5:
        return "CLIMAX"
    if multiplier >= 1.5:
        return "STRONG"
    if multiplier < 0.8:
        return "LOW"
    return "NORMAL"


def _volatility_state(bars: List[Any]) -> str:
    if len(bars) < 3:
        return "UNKNOWN"
    ranges = [pct_change(bar.h, bar.l) for bar in bars[-10:] if bar.l > 0]
    average_range = average(ranges) or 0.0
    if average_range >= 0.45:
        return "HIGH"
    if average_range <= 0.12:
        return "LOW"
    return "NORMAL"


def _is_opening_window(bars: List[Any]) -> bool:
    if not bars or not getattr(bars[-1], "t", None):
        return False
    timestamp = bars[-1].t
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    local = timestamp.astimezone(ET)
    minutes = (local.hour * 60 + local.minute) - (9 * 60 + 30)
    return 0 <= minutes <= 45


def evaluate_market_regime(
    market_bars: Optional[Dict[str, List[Any]]],
    config: Dict[str, Any],
    *,
    aapl_bars: Optional[List[Any]] = None,
    news_driven: bool = False,
) -> Dict[str, Any]:
    cfg = config.get("confirmation", {}).get("market_regime", {})
    lookback = max(3, int(cfg.get("market_regime_lookback_candles", 15)))
    choppy_cross_count = int(cfg.get("choppy_vwap_cross_count", 3))
    trend_min_score = int(cfg.get("trend_min_score", 65))
    market_bars = market_bars or {}
    spy = (market_bars.get("SPY") or [])[-lookback:]
    qqq = (market_bars.get("QQQ") or [])[-lookback:]
    aapl = (aapl_bars or market_bars.get("AAPL") or [])[-lookback:]
    spy_state = _market_state(spy, choppy_cross_count)
    qqq_state = _market_state(qqq, choppy_cross_count)
    aapl_state = _market_state(aapl, choppy_cross_count)

    reasons: List[str] = []
    warnings: List[str] = []
    score = int(round((spy_state["score"] + qqq_state["score"]) / 2))
    market_states = {spy_state["state"], qqq_state["state"]}
    market_directions = {spy_state["direction"], qqq_state["direction"]}
    volume_state = _volume_state(aapl)
    volatility_state = _volatility_state(aapl or spy or qqq)
    spy_alignment = _alignment(aapl_state["direction"], spy_state["direction"])
    qqq_alignment = _alignment(aapl_state["direction"], qqq_state["direction"])
    aapl_change = aapl_state.get("change_pct", 0.0)
    market_changes = [
        state.get("change_pct", 0.0)
        for state in (spy_state, qqq_state)
        if state.get("direction") != "UNKNOWN"
    ]
    relative_diff = aapl_change - (sum(market_changes) / len(market_changes) if market_changes else 0.0)
    if relative_diff >= 0.20:
        relative_strength = "STRONG"
    elif relative_diff <= -0.20:
        relative_strength = "WEAK"
    else:
        relative_strength = "NEUTRAL"

    regime = "UNKNOWN"
    if "UNKNOWN" in market_states:
        warnings.append("Market regime unavailable for SPY/QQQ")
    elif news_driven and volume_state in {"STRONG", "CLIMAX"} and volatility_state == "HIGH":
        regime = "NEWS_DRIVEN"
        score = max(score, 75)
        reasons.append("News context is active with high volume and volatility")
    elif (
        volume_state == "LOW"
        and abs(aapl_state.get("change_pct", 0.0)) >= 0.25
        and aapl_state.get("crosses", 0) >= 1
    ):
        regime = "LOW_VOLUME_FAKE_MOVE"
        score = max(score, 65)
        warnings.append("AAPL move lacks volume confirmation and may be a fake move")
    elif "CHOPPY" in market_states or (
        spy_state.get("crosses", 0) >= choppy_cross_count
        and qqq_state.get("crosses", 0) >= choppy_cross_count
    ):
        regime = "CHOPPY"
        score = max(score, 65)
        warnings.append("SPY/QQQ are repeatedly crossing VWAP")
    elif market_directions == {"BULLISH"} and score >= trend_min_score:
        regime = "OPENING_DRIVE_UP" if _is_opening_window(spy or qqq) else "TRENDING_UP"
        reasons.append("SPY and QQQ are above VWAP with rising EMA structure")
    elif market_directions == {"BEARISH"} and score >= trend_min_score:
        regime = "OPENING_DRIVE_DOWN" if _is_opening_window(spy or qqq) else "TRENDING_DOWN"
        reasons.append("SPY and QQQ are below VWAP with falling EMA structure")
    elif market_directions == {"NEUTRAL"} or (
        abs(spy_state.get("change_pct", 0.0)) < 0.15
        and abs(qqq_state.get("change_pct", 0.0)) < 0.15
    ):
        regime = "RANGE_BOUND"
        reasons.append("SPY and QQQ remain inside a narrow intraday range")
    elif market_directions == {"BULLISH", "BEARISH"}:
        regime = "REVERSAL_ATTEMPT"
        warnings.append("SPY and QQQ disagree; a market reversal may be developing")
    else:
        regime = "REVERSAL_ATTEMPT"
        warnings.append("Market direction is changing but not yet confirmed")

    regime_reason = (reasons or warnings or ["Insufficient evidence for a clear market regime"])[0]
    return {
        "market_regime": regime,
        "regime_score": int(max(0, min(100, score))),
        "market_score": int(max(0, min(100, score))),
        "regime_reason": regime_reason,
        "spy_alignment": spy_alignment,
        "qqq_alignment": qqq_alignment,
        "aapl_relative_strength": relative_strength,
        "volume_state": volume_state,
        "volatility_state": volatility_state,
        "spy_state": spy_state,
        "qqq_state": qqq_state,
        "aapl_state": aapl_state,
        "reasons": reasons[:6],
        "warnings": warnings[:6],
    }
