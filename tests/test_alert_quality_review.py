from __future__ import annotations

import json
from pathlib import Path

from tools.export_review_package import export_review_package
from tools.review_alert_quality import build_alert_quality_review, write_alert_quality_review


DAY = "2026-06-12"


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def record(**values):
    return {"timestamp": f"{DAY}T10:00:00-04:00", "symbol": "AAPL", **values}


def test_review_handles_missing_logs_and_writes_valid_outputs(tmp_path: Path):
    summary = build_alert_quality_review(DAY, tmp_path / "logs")
    assert summary["alert_volume"]["total_alerts_generated"] == 0
    paths = write_alert_quality_review(DAY, tmp_path / "logs", tmp_path / "exports")
    assert json.loads(paths["json"].read_text())["date"] == DAY
    assert "Alert Volume Summary" in paths["markdown"].read_text()


def test_review_counts_channels_tiers_and_flags_noisy_day(tmp_path: Path):
    logs = tmp_path / "logs"
    write_jsonl(logs / "alerts.jsonl", [record(primary_setup="Mixed Signal") for _ in range(3)])
    write_jsonl(
        logs / "alert_orchestrator.jsonl",
        [
            record(alert_type="MIXED_NO_TRADE", alert_priority_tier="TIER_2_DASHBOARD_ONLY", dashboard_only=True),
            record(alert_type="MIXED_NO_TRADE", alert_priority_tier="TIER_2_DASHBOARD_ONLY", dashboard_only=True),
            record(alert_type="MIXED_NO_TRADE", alert_priority_tier="TIER_2_DASHBOARD_ONLY", dashboard_only=True),
            record(alert_type="RISK_WARNING", alert_priority_tier="TIER_3_LOG_ONLY"),
        ],
    )
    write_jsonl(
        logs / "notification_status.jsonl",
        [record(channel="telegram", sent=True, alert_tier="TIER_2_DASHBOARD_ONLY") for _ in range(3)],
    )
    summary = build_alert_quality_review(
        DAY,
        logs,
        {"target_max_telegram_alerts_per_day": 1, "warn_if_risk_warning_ratio_above": 0.1},
    )
    assert summary["alert_volume"]["telegram_text_sent"] == 3
    assert summary["alert_volume"]["dashboard_only_alerts"] == 3
    assert summary["alert_volume"]["log_only_events"] == 1
    assert summary["noise"]["label"] == "HIGH"
    assert "MIXED_NO_TRADE" in summary["noisy_alert_types"]
    assert "MIXED_NO_TRADE" in summary["should_be_dashboard_only"]
    assert summary["recommendations"]


def test_review_counts_scheduled_context_zones_and_sweeps(tmp_path: Path):
    logs = tmp_path / "logs"
    write_jsonl(logs / "market_map_updates.jsonl", [record(sent=True)])
    write_jsonl(logs / "morning_playbook.jsonl", [record(sent=True)])
    write_jsonl(logs / "liquidity_sweeps.jsonl", [record(telegram_sent=True), record(telegram_eligible=False)])
    write_jsonl(
        logs / "supply_demand_zones.jsonl",
        [record(demand_zones=[{"quality_label": "Too Wide", "is_too_wide": True}, {"quality_label": "A Zone"}])],
    )
    summary = build_alert_quality_review(DAY, logs)
    assert summary["scheduled_context"]["market_map_updates_sent"] == 1
    assert summary["scheduled_context"]["morning_playbook_sent"] == 1
    assert summary["liquidity_sweeps"] == {"sent": 1, "suppressed": 1}
    assert summary["zones"] == {"too_wide": 1, "clean_quality": 1}


def test_review_package_includes_alert_quality_review(tmp_path: Path):
    logs = tmp_path / "logs"
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "dashboard_snapshot_latest.md").write_text("# snapshot")
    (snapshots / "dashboard_snapshot_latest.json").write_text("{}")
    config = tmp_path / "config.example.json"
    config.write_text("{}")
    write_jsonl(logs / "options_whale_scans.jsonl", [record(coverage_warning="AAPL coverage is stale")])
    write_jsonl(logs / "options_whale_episodes.jsonl", [record(episode_id="ep-1")])
    write_jsonl(logs / "options_oi_reviews.jsonl", [record(episode_id="ep-1", status="confirmed_opening", original_time=f"{DAY}T10:00:00-04:00"), record(episode_id="wrong-oi", original_time="2026-06-11T10:00:00-04:00")])
    write_jsonl(tmp_path / "data" / "options_whale_episode_outcomes.jsonl", [record(episode_id="ep-1", detected_at=f"{DAY}T10:00:00-04:00"), record(episode_id="wrong-outcome", detected_at="2026-06-11T10:00:00-04:00")])
    result = export_review_package(
        day_text=DAY,
        start_text="09:30",
        end_text="16:00",
        output_dir=tmp_path / "exports",
        log_dir=logs,
        snapshot_dir=snapshots,
        config_example=config,
    )
    assert result["alert_quality_markdown"].exists()
    assert result["alert_quality_json"].exists()
    assert result["zip"].exists()
    package = result["package_dir"]
    assert (package / "logs" / "options_whale_scans.jsonl").exists()
    assert (package / "logs" / "options_whale_episodes.jsonl").exists()
    assert (package / "logs" / "options_oi_reviews.jsonl").exists()
    assert (package / "data" / "options_whale_episode_outcomes.jsonl").exists()
    assert "wrong-oi" not in (package / "logs" / "options_oi_reviews.jsonl").read_text()
    assert "wrong-outcome" not in (package / "data" / "options_whale_episode_outcomes.jsonl").read_text()
    assert "Scan passes with coverage warnings: 1" in result["summary"].read_text()
