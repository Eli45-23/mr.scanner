from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base import pct_change, vwap


def _change_pct(bars: List[Any], lookback: int) -> Optional[float]:
    if len(bars) < 2:
        return None
    start_idx = max(0, len(bars) - lookback - 1)
    start = bars[start_idx].c
    end = bars[-1].c
    return pct_change(end, start)


def evaluate_relative_strength(
    symbol: str,
    bars: List[Any],
    config: Dict[str, Any],
    market_bars: Optional[Dict[str, List[Any]]] = None,
    *,
    direction: str = "neutral",
) -> Dict[str, Any]:
    cfg = config.get("confirmation", {}).get("relative_strength", {})
    lookback = max(1, int(cfg.get("rs_lookback_candles", 5)))
    strong_diff = float(cfg.get("rs_strong_diff_pct", 0.20))
    weak_diff = float(cfg.get("rs_weak_diff_pct", -0.20))
    confirm_symbols = cfg.get("market_confirm_symbols", ["SPY", "QQQ"])
    if not isinstance(confirm_symbols, list):
        confirm_symbols = ["SPY", "QQQ"]

    market_bars = market_bars or {}
    symbol_change = _change_pct(bars, lookback)
    if symbol_change is None:
        return {
            "relative_strength_score": 50,
            "relative_strength_label": "NEUTRAL",
            "symbol_change_pct": 0.0,
            "spy_change_pct": 0.0,
            "qqq_change_pct": 0.0,
            "symbol_vs_spy": 0.0,
            "symbol_vs_qqq": 0.0,
            "reasons": [],
            "warnings": ["Not enough candles to evaluate relative strength"],
        }

    comparisons: Dict[str, float] = {}
    for market_symbol in confirm_symbols:
        change = _change_pct(market_bars.get(market_symbol, []), lookback)
        if change is not None:
            comparisons[market_symbol] = change

    if not comparisons:
        return {
            "relative_strength_score": 50,
            "relative_strength_label": "NEUTRAL",
            "symbol_change_pct": round(symbol_change, 4),
            "spy_change_pct": 0.0,
            "qqq_change_pct": 0.0,
            "symbol_vs_spy": 0.0,
            "symbol_vs_qqq": 0.0,
            "reasons": [],
            "warnings": ["Market comparison bars unavailable for relative strength"],
        }

    avg_market_change = sum(comparisons.values()) / len(comparisons)
    diff = symbol_change - avg_market_change
    score = 50.0
    reasons: List[str] = []
    warnings: List[str] = []
    label = "NEUTRAL"

    symbol_vwap = vwap(bars)
    spy_change = comparisons.get("SPY", 0.0)
    qqq_change = comparisons.get("QQQ", 0.0)
    spy_bars = market_bars.get("SPY", [])
    qqq_bars = market_bars.get("QQQ", [])
    spy_vwap = vwap(spy_bars) if spy_bars else None
    qqq_vwap = vwap(qqq_bars) if qqq_bars else None
    symbol_above_vwap = bool(symbol_vwap and bars[-1].c > symbol_vwap)
    market_above_vwap = [
        bool(spy_vwap and spy_bars and spy_bars[-1].c > spy_vwap),
        bool(qqq_vwap and qqq_bars and qqq_bars[-1].c > qqq_vwap),
    ]

    if diff >= strong_diff:
        score += 22
        label = "STRONG"
        reasons.append("Symbol is outperforming SPY/QQQ")
    elif diff <= weak_diff:
        score -= 22
        label = "WEAK"
        warnings.append("Symbol is underperforming SPY/QQQ")

    if symbol_above_vwap and not all(market_above_vwap):
        score += 8
        label = "STRONG" if label == "NEUTRAL" else label
        reasons.append("Symbol is above VWAP while market confirmation is mixed")
    if not symbol_above_vwap and any(market_above_vwap):
        score -= 8
        if label == "NEUTRAL":
            label = "WEAK"
        warnings.append("Symbol is below VWAP while market is holding VWAP")

    if direction == "bullish" and label == "WEAK":
        warnings.append("Bullish setup lacks relative strength")
    if direction == "bearish" and label == "STRONG":
        warnings.append("Bearish setup is fighting relative strength")

    return {
        "relative_strength_score": int(max(0, min(100, round(score)))),
        "relative_strength_label": label,
        "symbol_change_pct": round(symbol_change, 4),
        "spy_change_pct": round(spy_change, 4),
        "qqq_change_pct": round(qqq_change, 4),
        "symbol_vs_spy": round(symbol_change - spy_change, 4),
        "symbol_vs_qqq": round(symbol_change - qqq_change, 4),
        "reasons": reasons[:6],
        "warnings": warnings[:6],
    }
