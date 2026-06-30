from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Protocol


def _candidate(row: Dict[str, Any]) -> Dict[str, Any]:
    return row.get("candidate") if isinstance(row.get("candidate"), dict) else row


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _option_symbol(row: Dict[str, Any]) -> str:
    c = _candidate(row)
    return str(c.get("option_symbol") or c.get("contract_symbol") or c.get("symbol") or "")


def _underlying_symbol(row: Dict[str, Any]) -> str:
    c = _candidate(row)
    return str(c.get("underlying_symbol") or c.get("underlying") or "").upper()


def _expiration(row: Dict[str, Any]) -> str:
    c = _candidate(row)
    return str(c.get("expiration") or c.get("expiration_date") or "")[:10]


def classify_next_day_oi(original: Dict[str, Any], next_day_open_interest: int | None) -> Dict[str, Any]:
    """Classify whether next-day OI confirms same-day flow.

    This does not use trade execution and does not create a trade signal. It only
    compares prior OI, same-day volume, and next-day OI for the same contract.
    """
    candidate = _candidate(original)
    prior = _safe_int(candidate.get("open_interest") if candidate.get("open_interest") is not None else original.get("open_interest"), 0)
    vol = _safe_int(candidate.get("volume") if candidate.get("volume") is not None else original.get("volume"), 0)
    if next_day_open_interest is None:
        return {
            "next_day_oi_status": "pending",
            "next_day_open_interest": None,
            "next_day_oi_change": None,
            "next_day_oi_change_percent": None,
            "next_day_oi_reason": "awaiting next trading day OI",
            "open_close_estimate_after_oi": original.get("open_close_estimate") or original.get("opening_flow_estimate") or "unknown",
        }
    next_oi = _safe_int(next_day_open_interest, 0)
    change = next_oi - prior
    change_pct = round((change / prior) * 100.0, 2) if prior > 0 else None
    opening_threshold = max(1, int(vol * 0.5)) if vol > 0 else max(1, int(prior * 0.25))
    not_confirmed_threshold = max(1, int(vol * 0.15)) if vol > 0 else 1

    if vol <= 0 and prior <= 0 and next_oi <= 0:
        status = "unavailable"
        estimate = "unknown"
        reason = "volume, prior OI, and next-day OI are unavailable or zero"
    elif change >= opening_threshold:
        status = "confirmed_opening"
        estimate = "confirmed_opening"
        reason = f"next-day OI increased by {change:,}, which is at least 50% of same-day volume ({vol:,})"
    elif change <= -not_confirmed_threshold:
        status = "likely_closing"
        estimate = "likely_closing"
        reason = f"next-day OI decreased by {abs(change):,}; same-day flow did not confirm as opening"
    elif abs(change) < not_confirmed_threshold:
        status = "not_confirmed"
        estimate = "not_confirmed"
        reason = f"next-day OI changed by only {change:,}, too small versus same-day volume ({vol:,}) to confirm opening"
    else:
        status = "not_confirmed"
        estimate = "not_confirmed"
        reason = f"next-day OI increased by {change:,}, but not enough versus same-day volume ({vol:,}) to confirm opening"

    return {
        "next_day_oi_status": status,
        "next_day_open_interest": next_oi,
        "next_day_oi_change": change,
        "next_day_oi_change_percent": change_pct,
        "next_day_oi_reason": reason,
        "open_close_estimate_after_oi": estimate,
        # Backward-compatible legacy fields.
        "open_interest_change": change,
        "likely_opening": status == "confirmed_opening",
        "likely_closing": estimate == "likely_closing",
        "likely_roll_or_unclear": estimate in {"not_confirmed", "unknown", "roll_or_spread_possible", "hedge_or_unclear"},
    }


def review_alerts_with_next_day_oi(alerts: Iterable[Dict[str, Any]], oi_by_contract: Dict[str, int]) -> List[Dict[str, Any]]:
    reviews: List[Dict[str, Any]] = []
    for alert in alerts:
        symbol = _option_symbol(alert)
        if not symbol:
            continue
        result = classify_next_day_oi(alert, oi_by_contract.get(symbol))
        members = alert.get("episode_member_contracts") if isinstance(alert.get("episode_member_contracts"), list) else []
        member_changes = []
        for member in members:
            member_symbol = str(member.get("option_symbol") or "")
            if member_symbol in oi_by_contract:
                member_changes.append(_safe_int(oi_by_contract[member_symbol]) - _safe_int(member.get("open_interest")))
        if len(member_changes) >= 2 and any(value > 0 for value in member_changes) and any(value < 0 for value in member_changes):
            result.update({"next_day_oi_status": "roll_or_spread_possible", "open_close_estimate_after_oi": "roll_or_spread_possible", "next_day_oi_reason": "Related episode contracts have offsetting next-day OI changes; a roll or spread is possible."})
        elif alert.get("possible_multileg") and result.get("next_day_oi_status") == "confirmed_opening":
            result.update({"next_day_oi_status": "hedge_or_unclear", "open_close_estimate_after_oi": "hedge_or_unclear", "next_day_oi_reason": "OI increased, but multi-leg structure means directional intent may be hedged."})
        if result["next_day_oi_status"] == "pending":
            continue
        candidate = _candidate(alert)
        reviews.append({
            "option_symbol": symbol,
            "underlying_symbol": _underlying_symbol(alert),
            "expiration": _expiration(alert),
            "option_type": candidate.get("option_type"),
            "strike": candidate.get("strike"),
            "prior_open_interest": candidate.get("open_interest") or alert.get("open_interest"),
            "same_day_volume": candidate.get("volume") or alert.get("volume"),
            "original_time": alert.get("time_detected") or alert.get("timestamp") or candidate.get("time_detected"),
            "episode_id": alert.get("flow_episode_id") or alert.get("episode_id"),
            **result,
        })
    return reviews


class ContractClient(Protocol):
    def get_option_contracts(
        self,
        *,
        expiration_gte: date,
        expiration_lte: date,
        underlying_symbols: Optional[List[str]] = None,
        limit: int = 10000,
        max_contracts: int = 10000,
    ) -> List[Dict[str, Any]]:
        ...


def _contract_symbol(row: Dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("option_symbol") or row.get("id") or "")


def _contract_oi(row: Dict[str, Any]) -> Optional[int]:
    raw = row.get("open_interest") or row.get("openInterest")
    if raw is None:
        return None
    return _safe_int(raw, 0)


def fetch_next_day_oi_map(client: ContractClient, alerts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Fetch current contract OI for alert contracts using the contracts endpoint."""
    grouped: Dict[tuple[str, str], List[str]] = {}
    for alert in alerts:
        symbol = _option_symbol(alert)
        underlying = _underlying_symbol(alert)
        exp = _expiration(alert)
        if symbol and underlying and exp:
            grouped.setdefault((underlying, exp), []).append(symbol)

    out: Dict[str, int] = {}
    for (underlying, exp), symbols in grouped.items():
        try:
            exp_date = date.fromisoformat(exp)
        except ValueError:
            continue
        wanted = set(symbols)
        rows = client.get_option_contracts(
            expiration_gte=exp_date,
            expiration_lte=exp_date,
            underlying_symbols=[underlying],
            limit=10000,
            max_contracts=10000,
        )
        for row in rows:
            symbol = _contract_symbol(row)
            oi = _contract_oi(row)
            if symbol in wanted and oi is not None:
                out[symbol] = oi
    return out
