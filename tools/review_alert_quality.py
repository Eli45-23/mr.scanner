#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


APP_DIR = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
DEFAULT_CONFIG = {
    "enabled": True,
    "target_max_telegram_alerts_per_day": 25,
    "warn_if_risk_warning_ratio_above": 0.5,
    "warn_if_chop_alert_ratio_above": 0.4,
    "include_recommendations": True,
}


def _timestamp(record: Dict[str, Any]) -> Optional[datetime]:
    raw = record.get("timestamp") or record.get("alert_timestamp") or record.get("time")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=ET)).astimezone(ET)


def read_day(path: Path, day_text: str) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _timestamp(record) if isinstance(record, dict) else None
        if ts and ts.date().isoformat() == day_text:
            records.append(record)
    return records


def _value(record: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return "UNKNOWN"


def _truthy(record: Dict[str, Any], *keys: str) -> bool:
    return any(record.get(key) is True for key in keys)


def _zone_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    zones: List[Dict[str, Any]] = []
    for record in records:
        for key in ("demand_zones", "supply_zones", "zones"):
            value = record.get(key)
            if isinstance(value, list):
                zones.extend(item for item in value if isinstance(item, dict))
    return zones


def build_alert_quality_review(day_text: str, log_dir: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = {**DEFAULT_CONFIG, **(config or {})}
    logs = {
        name: read_day(log_dir / filename, day_text)
        for name, filename in {
            "alerts": "alerts.jsonl",
            "notifications": "notification_status.jsonl",
            "orchestrator": "alert_orchestrator.jsonl",
            "chop": "chop_mode.jsonl",
            "sweeps": "liquidity_sweeps.jsonl",
            "market_maps": "market_map_updates.jsonl",
            "playbooks": "morning_playbook.jsonl",
            "missed": "missed_clean_entry.jsonl",
            "zones": "supply_demand_zones.jsonl",
            "performance": "post_alert_performance.jsonl",
        }.items()
    }
    alerts = logs["alerts"]
    decisions = logs["orchestrator"] or alerts
    notifications = logs["notifications"]
    telegram_sent = [
        record for record in notifications
        if str(record.get("channel") or "").lower() == "telegram" and _truthy(record, "sent", "telegram_sent")
    ]
    sms_sent = [record for record in notifications if str(record.get("channel") or "").lower() == "sms" and record.get("sent")]
    tiers = Counter(_value(record, "alert_priority_tier", "priority_tier", "decision_tier", "alert_tier") for record in decisions)
    alert_types = Counter(_value(record, "alert_type", "final_alert_type", "phone_conclusion", "primary_setup") for record in decisions)
    blocked_reasons = Counter(
        _value(record, "suppression_reason", "block_reason", "alert_priority_reason", "telegram_suppressed_reason")
        for record in decisions
        if not _truthy(record, "telegram_allowed", "orchestrator_telegram_allowed", "alert_priority_telegram_allowed")
    )
    dashboard_only = sum(
        1 for record in decisions
        if _truthy(record, "dashboard_only", "orchestrator_dashboard_allowed", "alert_priority_dashboard_only")
        and not _truthy(record, "telegram_allowed", "orchestrator_telegram_allowed", "alert_priority_telegram_allowed")
    )
    log_only = sum(1 for record in decisions if _value(record, "alert_priority_tier", "priority_tier") == "TIER_3_LOG_ONLY")
    text = [json.dumps(record, sort_keys=True).lower() for record in decisions]
    mixed_count = sum("mixed" in value for value in text)
    do_not_chase_count = sum("do_not_chase" in value or "do not chase" in value for value in text)
    late_count = sum('"late"' in value or "late move" in value for value in text)
    chop_count = sum(1 for record in logs["chop"] if record.get("chop_mode_active"))
    sweep_sent = sum(1 for record in logs["sweeps"] if record.get("telegram_sent"))
    sweep_suppressed = sum(1 for record in logs["sweeps"] if record.get("telegram_eligible") is False or record.get("telegram_filter_allowed") is False)
    tier1_sent = sum(
        1 for record in decisions
        if _value(record, "alert_priority_tier", "priority_tier") == "TIER_1_TEXT_ALERT"
        and _truthy(record, "telegram_allowed", "orchestrator_telegram_allowed", "alert_priority_telegram_allowed")
    )
    completed_tier1 = [
        record for record in logs["performance"]
        if _value(record, "alert_tier", "priority_tier") == "TIER_1_TEXT_ALERT"
        and record.get("direction_correct") is not None
    ]
    tier1_follow = sum(1 for record in completed_tier1 if record.get("direction_correct") or record.get("useful_alert"))
    zones = _zone_records(logs["zones"])
    too_wide = sum(1 for zone in zones if zone.get("is_too_wide") or "too wide" in _value(zone, "quality_label", "label").lower())
    clean_zones = sum(1 for zone in zones if _value(zone, "quality_label", "label") in {"A+ Zone", "A Zone"})
    telegram_count = len(telegram_sent) + len(sms_sent)
    risk_warning_count = alert_types.get("RISK_WARNING", 0) + tiers.get("RISK_WARNING", 0)
    total = max(1, len(decisions))
    noisy_tier_messages = sum(
        1 for record in telegram_sent
        if _value(record, "alert_priority_tier", "priority_tier", "alert_tier") in {"TIER_2_DASHBOARD_ONLY", "TIER_3_LOG_ONLY"}
    )
    reasons: List[str] = []
    if telegram_count > int(settings["target_max_telegram_alerts_per_day"]):
        reasons.append("Telegram/text volume exceeded the daily target.")
    if risk_warning_count / total > float(settings["warn_if_risk_warning_ratio_above"]):
        reasons.append("Risk warnings dominated alert decisions.")
    if chop_count / total > float(settings["warn_if_chop_alert_ratio_above"]):
        reasons.append("Chop-mode activity dominated the day.")
    if noisy_tier_messages:
        reasons.append("Tier 2/3 context reached Telegram.")
    noise_score = min(100, round(telegram_count * 2 + noisy_tier_messages * 15 + (risk_warning_count / total) * 30 + (chop_count / total) * 30))
    noise_label = "HIGH" if reasons or noise_score >= 60 else ("MEDIUM" if noise_score >= 30 else "LOW")
    recommendations = []
    noisy_alert_types = [
        name for name, count in alert_types.most_common()
        if name != "UNKNOWN" and count >= max(3, round(len(decisions) * 0.25))
    ]
    dashboard_only_candidates = [
        name for name in noisy_alert_types
        if any(term in name.upper() for term in ("MIXED", "CHOP", "DO_NOT_CHASE", "LATE", "RISK_WARNING"))
    ]
    if telegram_count > int(settings["target_max_telegram_alerts_per_day"]):
        recommendations.append("Keep more repeated context and warning events dashboard-only.")
    if mixed_count + do_not_chase_count + late_count > max(5, len(decisions) // 2):
        recommendations.append("Review repeated mixed/do-not-chase/late events for stronger dedupe.")
    if sweep_suppressed < sweep_sent:
        recommendations.append("Review whether confirmed sweep alerts still need a higher confidence threshold.")
    if too_wide:
        recommendations.append("Review Too Wide zones before using them as precision references.")
    if not recommendations:
        recommendations.append("No immediate alert-noise changes are indicated by the available logs.")
    useful = [record for record in logs["performance"] if record.get("useful_alert")]
    best_setup = Counter(_value(record, "setup_type") for record in useful).most_common(1)
    return {
        "date": day_text,
        "alert_volume": {
            "total_alerts_generated": len(alerts),
            "total_decisions": len(decisions),
            "telegram_text_sent": telegram_count,
            "dashboard_only_alerts": dashboard_only,
            "log_only_events": log_only,
            "alerts_by_type": dict(alert_types),
            "alerts_by_priority_tier": dict(tiers),
            "blocked_alerts_by_reason": dict(blocked_reasons),
        },
        "noise": {"label": noise_label, "score": noise_score, "reasons": reasons},
        "warnings": {
            "chop_mode_alert_count": chop_count,
            "mixed_signal_count": mixed_count,
            "do_not_chase_count": do_not_chase_count,
            "late_move_count": late_count,
        },
        "liquidity_sweeps": {"sent": sweep_sent, "suppressed": sweep_suppressed},
        "scheduled_context": {
            "market_map_updates_sent": sum(1 for record in logs["market_maps"] if record.get("sent")),
            "morning_playbook_sent": sum(1 for record in logs["playbooks"] if record.get("sent")),
        },
        "tier_1": {
            "sent": tier1_sent,
            "follow_through_measured": len(completed_tier1),
            "follow_through_positive": tier1_follow,
        },
        "zones": {"too_wide": too_wide, "clean_quality": clean_zones},
        "missed_clean_entries": len(logs["missed"]),
        "best_clean_setup": best_setup[0][0] if best_setup else "unavailable",
        "noisy_alert_types": noisy_alert_types,
        "should_be_dashboard_only": dashboard_only_candidates,
        "recommendations": recommendations if settings.get("include_recommendations", True) else [],
        "context_only": True,
        "can_approve_trades": False,
    }


def render_alert_quality_markdown(summary: Dict[str, Any]) -> str:
    volume, noise = summary["alert_volume"], summary["noise"]
    recommendations = "\n".join(f"- {item}" for item in summary["recommendations"]) or "- Recommendations disabled."
    blocked = "\n".join(f"- {key}: {value}" for key, value in sorted(volume["blocked_alerts_by_reason"].items())) or "- None recorded."
    return f"""# Alert Quality Review — {summary["date"]}

## Alert Volume Summary
- Alerts generated: {volume["total_alerts_generated"]}
- Decisions reviewed: {volume["total_decisions"]}
- Telegram/text sent: {volume["telegram_text_sent"]}
- Dashboard-only alerts: {volume["dashboard_only_alerts"]}
- Log-only events: {volume["log_only_events"]}
- Alerts by type: {json.dumps(volume["alerts_by_type"], sort_keys=True)}
- Alerts by priority tier: {json.dumps(volume["alerts_by_priority_tier"], sort_keys=True)}

## Telegram Noise Score
- Noise: {noise["label"]} ({noise["score"]}/100)
- Reasons: {"; ".join(noise["reasons"]) if noise["reasons"] else "No major noise condition detected."}

## Dashboard-Only Context Summary
{blocked}
- Noisy alert types: {", ".join(summary["noisy_alert_types"]) if summary["noisy_alert_types"] else "None identified"}
- Candidates to keep dashboard-only: {", ".join(summary["should_be_dashboard_only"]) if summary["should_be_dashboard_only"] else "None identified"}

## Tier 1 Alert Review
- Tier 1 sent: {summary["tier_1"]["sent"]}
- Follow-through measured: {summary["tier_1"]["follow_through_measured"]}
- Positive follow-through: {summary["tier_1"]["follow_through_positive"]}

## Chop / Mixed / Do-Not-Chase Review
- Chop-mode alerts: {summary["warnings"]["chop_mode_alert_count"]}
- Mixed signals: {summary["warnings"]["mixed_signal_count"]}
- Do-not-chase: {summary["warnings"]["do_not_chase_count"]}
- Late moves: {summary["warnings"]["late_move_count"]}

## Liquidity Sweep Review
- Sweep alerts sent: {summary["liquidity_sweeps"]["sent"]}
- Sweep alerts suppressed: {summary["liquidity_sweeps"]["suppressed"]}

## Zone Quality Review
- Too Wide zones: {summary["zones"]["too_wide"]}
- A/A+ clean zones: {summary["zones"]["clean_quality"]}

## Missed Clean Entry Review
- Missed clean entries: {summary["missed_clean_entries"]}
- Best clean setup: {summary["best_clean_setup"]}

## Recommended Changes For Tomorrow
{recommendations}

_Retrospective context only. This report does not change live scanner decisions._
"""


def write_alert_quality_review(day_text: str, log_dir: Path, output_dir: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    summary = build_alert_quality_review(day_text, Path(log_dir), config=config)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"alert_quality_review_{day_text}.json"
    markdown_path = output_dir / f"alert_quality_review_{day_text}.md"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_alert_quality_markdown(summary), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the daily Mr. Scanner alert-quality and noise review.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--log-dir", default=str(APP_DIR / "logs"))
    parser.add_argument("--output-dir", default=str(APP_DIR / "exports"))
    args = parser.parse_args()
    paths = write_alert_quality_review(args.date, Path(args.log_dir), Path(args.output_dir))
    print(f"Markdown: {paths['markdown']}")
    print(f"JSON: {paths['json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
