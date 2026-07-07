from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
DEFAULT_INTERVALS = (1, 3, 5, 10, 15)


def _normalized_setup(value: Any) -> str:
    return " ".join(str(value or "UNKNOWN").strip().upper().replace("_", " ").split())


def is_context_only_setup(alert: Any) -> bool:
    setup = _normalized_setup(
        _alert_value(alert, "setup_name")
        or _alert_value(alert, "primary_setup")
        or _alert_value(alert, "category")
    )
    return setup == "MIXED SIGNAL" or bool(_alert_value(alert, "mixed_signal_detected"))


def canonical_episode_key(alert: Any) -> str:
    timestamp = _parse_dt(_alert_value(alert, "timestamp")) or datetime.now(UTC)
    symbol = str(_alert_value(alert, "symbol", "")).strip().upper()
    setup = _normalized_setup(
        _alert_value(alert, "setup_name")
        or _alert_value(alert, "primary_setup")
        or _alert_value(alert, "category")
    )
    direction = _direction(
        _alert_value(alert, "setup_direction")
        or _alert_value(alert, "scenario_direction")
        or _alert_value(alert, "direction")
        or _alert_value(alert, "strategy_direction")
    )
    return "|".join((timestamp.astimezone(ET).date().isoformat(), symbol, setup, direction))


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _direction(value: Any) -> str:
    text = str(value or "").upper()
    return text if text in {"BULLISH", "BEARISH"} else "NEUTRAL"


def _signed_move_pct(price: float, alert_price: float, direction: str) -> float:
    raw = (price - alert_price) / alert_price * 100.0 if alert_price else 0.0
    return -raw if direction == "BEARISH" else raw


def _alert_value(alert: Any, name: str, default: Any = None) -> Any:
    if isinstance(alert, dict):
        return alert.get(name, default)
    return getattr(alert, name, default)


def alert_tracking_record(alert: Any, target_move_pct: float = 0.30, meaningful_move_pct: float = 0.10) -> Dict[str, Any]:
    timestamp = _parse_dt(_alert_value(alert, "timestamp")) or datetime.now(UTC)
    symbol = str(_alert_value(alert, "symbol", "")).upper()
    direction = _direction(
        _alert_value(alert, "setup_direction")
        or _alert_value(alert, "scenario_direction")
        or _alert_value(alert, "direction")
        or _alert_value(alert, "strategy_direction")
    )
    setup_type = (
        _alert_value(alert, "setup_name")
        or _alert_value(alert, "primary_setup")
        or _alert_value(alert, "category")
        or "Unknown"
    )
    identity = canonical_episode_key(alert)
    return {
        "alert_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "canonical_episode_key": identity,
        "alert_timestamp": timestamp.isoformat(),
        "last_updated_at": timestamp.isoformat(),
        "status": "PENDING",
        "symbol": symbol,
        "direction": direction,
        "price_at_alert": float(_alert_value(alert, "price") or 0.0),
        "setup_type": str(setup_type),
        "alert_tier": _alert_value(alert, "alert_tier"),
        "market_regime": _alert_value(alert, "market_regime"),
        "option_quality_at_alert": _alert_value(alert, "option_quality"),
        "entry_timing_at_alert": _alert_value(alert, "entry_timing_label")
        or _alert_value(alert, "entry_quality_label"),
        "invalidation_level": _alert_value(alert, "invalidation_level"),
        "target_move_pct": float(target_move_pct),
        "meaningful_move_threshold_pct": float(meaningful_move_pct),
        "outcome_definition_version": f"v2_meaningful_move_{float(meaningful_move_pct):.2f}",
        "orchestrator_final_alert_type": _alert_value(alert, "orchestrator_final_alert_type"),
        "orchestrator_decision_reason": _alert_value(alert, "orchestrator_decision_reason"),
        "delivery_requested": bool(_alert_value(alert, "phase3_delivery_requested")),
        "delivery_attempted": bool(_alert_value(alert, "phase3_delivery_attempted")),
        "delivery_succeeded": bool(_alert_value(alert, "phase3_delivery_succeeded")),
        "delivery_final_decision": _alert_value(alert, "phase3_heads_up_final_decision"),
        "option_quality_reasons": list(_alert_value(alert, "option_quality_reasons") or []),
        "decision_timestamp": _alert_value(alert, "decision_timestamp") or timestamp.isoformat(),
        "notification_eligible_at": timestamp.isoformat() if (_alert_value(alert, "phase3_delivery_requested") or _alert_value(alert, "sms_allowed") or _alert_value(alert, "watch_allowed")) else None,
        "delivery_attempted_at": timestamp.isoformat() if _alert_value(alert, "phase3_delivery_attempted") else None,
        "delivery_succeeded_at": timestamp.isoformat() if _alert_value(alert, "phase3_delivery_succeeded") else None,
        "interval_prices": {},
        "interval_moves_pct": {},
        "max_favorable_excursion_pct": None,
        "max_adverse_excursion_pct": None,
        "direction_correct": None,
        "alert_was_early": None,
        "alert_was_late": None,
        "hit_invalidation": False,
        "hit_target_zone": False,
        "useful_alert": None,
        "should_be_blocked_next_time": None,
    }


def update_performance_record(
    record: Dict[str, Any],
    bars: Iterable[Any],
    *,
    now: Optional[datetime] = None,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
) -> Dict[str, Any]:
    interval_values = tuple(sorted({int(value) for value in intervals if int(value) > 0})) or DEFAULT_INTERVALS
    alert_time = _parse_dt(record.get("alert_timestamp"))
    alert_price = float(record.get("price_at_alert") or 0.0)
    if not alert_time or alert_price <= 0:
        return record
    current = _parse_dt(now) or datetime.now(UTC)
    direction = _direction(record.get("direction"))
    normalized: List[Dict[str, Any]] = []
    for bar in bars:
        raw = asdict(bar) if is_dataclass(bar) else dict(bar) if isinstance(bar, dict) else {}
        bar_time = _parse_dt(raw.get("t") or raw.get("timestamp") or getattr(bar, "t", None))
        if not bar_time or bar_time < alert_time:
            continue
        normalized.append(
            {
                "t": bar_time,
                "h": float(raw.get("h", getattr(bar, "h", 0.0)) or 0.0),
                "l": float(raw.get("l", getattr(bar, "l", 0.0)) or 0.0),
                "c": float(raw.get("c", getattr(bar, "c", 0.0)) or 0.0),
            }
        )
    normalized.sort(key=lambda item: item["t"])
    if not normalized:
        return record

    interval_prices = dict(record.get("interval_prices") or {})
    interval_moves = dict(record.get("interval_moves_pct") or {})
    for minute in interval_values:
        key = f"{int(minute)}m"
        target_time = alert_time + timedelta(minutes=int(minute))
        if key in interval_prices or current < target_time:
            continue
        candidate = next((bar for bar in normalized if bar["t"] >= target_time and bar["c"] > 0), None)
        if candidate:
            interval_prices[key] = round(candidate["c"], 4)
            interval_moves[key] = round(_signed_move_pct(candidate["c"], alert_price, direction), 4)

    observed_end = min(current, alert_time + timedelta(minutes=max(interval_values)))
    observed = [bar for bar in normalized if bar["t"] <= observed_end]
    favorable: List[float] = []
    adverse: List[float] = []
    invalidation = record.get("invalidation_level")
    hit_invalidation = bool(record.get("hit_invalidation"))
    for bar in observed:
        if direction == "BEARISH":
            favorable.append(_signed_move_pct(bar["l"], alert_price, direction))
            adverse.append(max(0.0, -_signed_move_pct(bar["h"], alert_price, direction)))
            if invalidation is not None and bar["h"] >= float(invalidation):
                hit_invalidation = True
        else:
            favorable.append(_signed_move_pct(bar["h"], alert_price, direction))
            adverse.append(max(0.0, -_signed_move_pct(bar["l"], alert_price, direction)))
            if invalidation is not None and bar["l"] <= float(invalidation):
                hit_invalidation = True

    mfe = max([0.0, *favorable])
    mae = max([0.0, *adverse])
    target = float(record.get("target_move_pct") or 0.30)
    meaningful = float(record.get("meaningful_move_threshold_pct") or 0.10)
    final_key = next((f"{minute}m" for minute in reversed(interval_values) if f"{minute}m" in interval_moves), None)
    final_move = float(interval_moves.get(final_key, 0.0)) if final_key else None
    direction_correct = final_move > 0 if final_move is not None else None
    first_move = interval_moves.get("1m")
    entry_timing = str(record.get("entry_timing_at_alert") or "").upper()
    record.update(
        {
            "last_updated_at": current.isoformat(),
            "interval_prices": interval_prices,
            "interval_moves_pct": interval_moves,
            "max_favorable_excursion_pct": round(mfe, 4),
            "max_adverse_excursion_pct": round(mae, 4),
            "direction_correct": direction_correct,
            "alert_was_early": bool(entry_timing in {"EARLY", "GOOD_POSITION"} and mfe >= meaningful),
            "alert_was_late": entry_timing in {"LATE", "DO_NOT_CHASE"},
            "hit_invalidation": hit_invalidation,
            "hit_target_zone": mfe >= target,
            "useful_alert": bool(final_move is not None and final_move >= meaningful and mfe >= meaningful and not hit_invalidation)
            if direction_correct is not None
            else None,
            "should_be_blocked_next_time": bool(hit_invalidation or (final_move <= -meaningful and mae >= meaningful))
            if final_move is not None
            else None,
            "outcome_reason": (
                "invalidation hit" if hit_invalidation else
                f"adverse move exceeded {meaningful:.2f}% and final signed move was negative" if final_move is not None and final_move <= -meaningful and mae >= meaningful else
                f"final signed move reached +{meaningful:.2f}% without invalidation" if final_move is not None and final_move >= meaningful and not hit_invalidation else
                "no meaningful directional move"
            ),
            "status": "COMPLETE" if f"{max(interval_values)}m" in interval_prices else "PENDING",
        }
    )
    return record


class PostAlertPerformanceTracker:
    def __init__(
        self,
        log_path: Path,
        state_path: Path,
        *,
        intervals: Iterable[int] = DEFAULT_INTERVALS,
        target_move_pct: float = 0.30,
        episode_path: Optional[Path] = None,
        meaningful_move_pct: float = 0.10,
    ) -> None:
        self.log_path = log_path
        self.state_path = state_path
        self.intervals = tuple(sorted({int(value) for value in intervals if int(value) > 0})) or DEFAULT_INTERVALS
        self.target_move_pct = float(target_move_pct)
        self.episode_path = episode_path
        self.meaningful_move_pct = float(meaningful_move_pct)
        self.pending: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.pending = {key: value for key, value in payload.items() if isinstance(value, dict)}
        except (OSError, json.JSONDecodeError):
            self.pending = {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.pending, indent=2, sort_keys=True), encoding="utf-8")

    def _log(self, record: Dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        if self.episode_path:
            self.episode_path.parent.mkdir(parents=True, exist_ok=True)
            with self.episode_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"canonical_episode_id": record.get("alert_id"), "episode_updated_at": record.get("last_updated_at"), **record}, sort_keys=True) + "\n")

    @staticmethod
    def _material_signature(record: Dict[str, Any]) -> tuple[Any, ...]:
        return (
            record.get("alert_tier"), record.get("market_regime"), record.get("option_quality_at_alert"),
            record.get("entry_timing_at_alert"), record.get("orchestrator_final_alert_type"),
            record.get("delivery_requested"), record.get("delivery_attempted"), record.get("delivery_succeeded"),
        )

    def register(self, alert: Any) -> Dict[str, Any]:
        if is_context_only_setup(alert):
            return {"registration_status": "CONTEXT_ONLY", "suppression_reason": "Mixed Signal is dashboard-only"}
        record = alert_tracking_record(alert, self.target_move_pct, self.meaningful_move_pct)
        prior = self.pending.get(record["alert_id"])
        if prior:
            if self._material_signature(prior) == self._material_signature(record):
                return {**prior, "registration_status": "SUPPRESSED_REPEAT", "suppression_reason": "No material canonical state change"}
            record = {
                **prior,
                **{key: value for key, value in record.items() if value is not None},
                "alert_timestamp": prior.get("alert_timestamp"),
                "last_updated_at": record.get("last_updated_at"),
                "canonical_update_type": "MATERIAL_CHANGE",
            }
        else:
            record["canonical_update_type"] = "INITIAL"
        record["registration_status"] = record["canonical_update_type"]
        self.pending[record["alert_id"]] = record
        self._log(record)
        self._save()
        return record

    def update(self, snapshots: Dict[str, Any], *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        updated: List[Dict[str, Any]] = []
        for alert_id, record in list(self.pending.items()):
            snapshot = snapshots.get(record.get("symbol"))
            bars = getattr(snapshot, "recent_bars", []) if snapshot is not None else []
            before = json.dumps(record, sort_keys=True)
            update_performance_record(record, bars, now=now, intervals=self.intervals)
            if json.dumps(record, sort_keys=True) != before:
                self._log(record)
                updated.append(dict(record))
            if record.get("status") == "COMPLETE":
                self.pending.pop(alert_id, None)
        if updated:
            self._save()
        return updated
