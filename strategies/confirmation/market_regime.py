from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base import ema, pct_change, vwap


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


def _market_state(bars: List[Any], choppy_cross_count: int) -> Dict[str, Any]:
    if len(bars) < 3:
        return {"state": "UNKNOWN", "score": 0, "above_vwap": False, "ema_rising": False, "crosses": 0}
    current_vwap = vwap(bars)
    current_ema = ema([bar.c for bar in bars], 9)
    prior_ema = ema([bar.c for bar in bars[:-1]], 9) if len(bars) > 2 else current_ema
    above_vwap = bool(current_vwap and bars[-1].c > current_vwap)
    below_vwap = bool(current_vwap and bars[-1].c < current_vwap)
    ema_rising = bool(current_ema and prior_ema and current_ema > prior_ema)
    ema_falling = bool(current_ema and prior_ema and current_ema < prior_ema)
    higher_high = bars[-1].h > max(bar.h for bar in bars[:-1])
    higher_low = bars[-1].l > min(bar.l for bar in bars[:-1])
    lower_low = bars[-1].l < min(bar.l for bar in bars[:-1])
    lower_high = bars[-1].h < max(bar.h for bar in bars[:-1])
    crosses = _vwap_cross_count(bars, current_vwap)
    move = pct_change(bars[-1].c, bars[0].c)

    bull_score = 0
    bear_score = 0
    if above_vwap:
        bull_score += 25
    if below_vwap:
        bear_score += 25
    if ema_rising:
        bull_score += 20
    if ema_falling:
        bear_score += 20
    if higher_high and higher_low:
        bull_score += 20
    if lower_low and lower_high:
        bear_score += 20
    if move > 0.15:
        bull_score += 15
    if move < -0.15:
        bear_score += 15
    if crosses >= choppy_cross_count:
        return {"state": "CHOPPY", "score": max(bull_score, bear_score), "above_vwap": above_vwap, "ema_rising": ema_rising, "crosses": crosses}
    if bull_score > bear_score:
        return {"state": "BULLISH", "score": bull_score, "above_vwap": above_vwap, "ema_rising": ema_rising, "crosses": crosses}
    if bear_score > bull_score:
        return {"state": "BEARISH", "score": bear_score, "above_vwap": above_vwap, "ema_rising": ema_rising, "crosses": crosses}
    return {"state": "FLAT", "score": max(bull_score, bear_score), "above_vwap": above_vwap, "ema_rising": ema_rising, "crosses": crosses}


def evaluate_market_regime(
    market_bars: Optional[Dict[str, List[Any]]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = config.get("confirmation", {}).get("market_regime", {})
    lookback = max(3, int(cfg.get("market_regime_lookback_candles", 15)))
    choppy_cross_count = int(cfg.get("choppy_vwap_cross_count", 3))
    trend_min_score = int(cfg.get("trend_min_score", 65))
    market_bars = market_bars or {}
    spy = (market_bars.get("SPY") or [])[-lookback:]
    qqq = (market_bars.get("QQQ") or [])[-lookback:]
    spy_state = _market_state(spy, choppy_cross_count)
    qqq_state = _market_state(qqq, choppy_cross_count)

    reasons: List[str] = []
    warnings: List[str] = []
    states = {spy_state["state"], qqq_state["state"]}
    score = int(round((spy_state["score"] + qqq_state["score"]) / 2))
    regime = "UNKNOWN"

    if "UNKNOWN" in states:
        warnings.append("Market regime unavailable for SPY/QQQ")
    elif "CHOPPY" in states:
        regime = "CHOPPY"
        warnings.append("Market is choppy around VWAP")
    elif states == {"BULLISH"} and score >= trend_min_score:
        regime = "BULL_TREND"
        reasons.append("SPY and QQQ are aligned in a bull trend")
    elif states == {"BEARISH"} and score >= trend_min_score:
        regime = "BEAR_TREND"
        reasons.append("SPY and QQQ are aligned in a bear trend")
    elif len(states) > 1:
        regime = "MIXED"
        warnings.append("Market confirmation is mixed/choppy")
    else:
        regime = "MIXED"
        warnings.append("Market trend is not strong enough to confirm")

    return {
        "market_regime": regime,
        "market_score": int(max(0, min(100, score))),
        "spy_state": spy_state,
        "qqq_state": qqq_state,
        "reasons": reasons[:6],
        "warnings": warnings[:6],
    }
