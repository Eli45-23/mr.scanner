#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


APP_DIR = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def read_latest_records(path: Path, day_text: str) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        timestamp = _parse_dt(record.get("alert_timestamp") or record.get("timestamp"))
        if timestamp and timestamp.date().isoformat() == day_text and record.get("alert_id"):
            latest[str(record["alert_id"])] = record
    return sorted(latest.values(), key=lambda item: str(item.get("alert_timestamp") or ""))


def _group_summary(records: Iterable[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[str(record.get(field) or "UNKNOWN")].append(record)
    rows: List[Dict[str, Any]] = []
    for name, items in groups.items():
        completed = [item for item in items if item.get("direction_correct") is not None]
        correct = sum(1 for item in completed if item.get("direction_correct"))
        useful = sum(1 for item in completed if item.get("useful_alert"))
        blocked = sum(1 for item in completed if item.get("should_be_blocked_next_time"))
        moves = [
            float((item.get("interval_moves_pct") or {}).get("15m"))
            for item in completed
            if (item.get("interval_moves_pct") or {}).get("15m") is not None
        ]
        rows.append(
            {
                "name": name,
                "alerts": len(items),
                "completed": len(completed),
                "accuracy_pct": round(correct / len(completed) * 100.0, 2) if completed else 0.0,
                "useful_pct": round(useful / len(completed) * 100.0, 2) if completed else 0.0,
                "block_next_pct": round(blocked / len(completed) * 100.0, 2) if completed else 0.0,
                "average_15m_move_pct": round(sum(moves) / len(moves), 4) if moves else None,
            }
        )
    return sorted(rows, key=lambda item: (-item["accuracy_pct"], -item["useful_pct"], item["name"]))


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed = [record for record in records if record.get("direction_correct") is not None]
    setup_rows = _group_summary(records, "setup_type")
    tier_rows = _group_summary(records, "alert_tier")
    moves = [
        float((record.get("interval_moves_pct") or {}).get("15m"))
        for record in completed
        if (record.get("interval_moves_pct") or {}).get("15m") is not None
    ]
    best_setup = setup_rows[0]["name"] if setup_rows else "unavailable"
    worst_setup = setup_rows[-1]["name"] if setup_rows else "unavailable"
    accurate_tier = tier_rows[0]["name"] if tier_rows else "unavailable"
    noisy_tier = max(tier_rows, key=lambda row: row["block_next_pct"])["name"] if tier_rows else "unavailable"
    return {
        "alerts": len(records),
        "completed": len(completed),
        "best_setup_type": best_setup,
        "worst_setup_type": worst_setup,
        "most_accurate_alert_tier": accurate_tier,
        "noisiest_alert_tier": noisy_tier,
        "average_move_after_alert_pct": round(sum(moves) / len(moves), 4) if moves else None,
        "late_alerts": [record for record in records if record.get("alert_was_late")],
        "useful_alerts": [record for record in records if record.get("useful_alert")],
        "alerts_to_block_next_time": [record for record in records if record.get("should_be_blocked_next_time")],
        "setup_summary": setup_rows,
        "tier_summary": tier_rows,
    }


def _table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "_No records available._"
    lines = [
        "| Name | Alerts | Completed | Accuracy | Useful | Block Next | Avg 15m Move |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        move = "unavailable" if row["average_15m_move_pct"] is None else f"{row['average_15m_move_pct']:.3f}%"
        lines.append(
            f"| {row['name']} | {row['alerts']} | {row['completed']} | {row['accuracy_pct']:.1f}% | "
            f"{row['useful_pct']:.1f}% | {row['block_next_pct']:.1f}% | {move} |"
        )
    return "\n".join(lines)


def _alert_list(records: List[Dict[str, Any]]) -> str:
    if not records:
        return "- None"
    return "\n".join(
        f"- {record.get('alert_timestamp')} | {record.get('symbol')} | {record.get('setup_type')} | "
        f"{record.get('alert_tier')} | MFE {record.get('max_favorable_excursion_pct')}% | "
        f"MAE {record.get('max_adverse_excursion_pct')}%"
        for record in records
    )


def build_report(day_text: str, records: List[Dict[str, Any]]) -> str:
    summary = summarize(records)
    avg_move = summary["average_move_after_alert_pct"]
    return f"""# Post-Alert Performance Review — {day_text}

## Summary
- Alerts tracked: {summary["alerts"]}
- Completed through available intervals: {summary["completed"]}
- Best setup type: {summary["best_setup_type"]}
- Worst setup type: {summary["worst_setup_type"]}
- Most accurate alert tier: {summary["most_accurate_alert_tier"]}
- Noisiest alert tier: {summary["noisiest_alert_tier"]}
- Average signed 15-minute move after alert: {"unavailable" if avg_move is None else f"{avg_move:.4f}%"}

## Setup Performance
{_table(summary["setup_summary"])}

## Alert Tier Performance
{_table(summary["tier_summary"])}

## Useful Alerts
{_alert_list(summary["useful_alerts"])}

## Late Alerts
{_alert_list(summary["late_alerts"])}

## Alerts That Should Be Blocked Next Time
{_alert_list(summary["alerts_to_block_next_time"])}

## Measurement Notes
- Direction correctness uses the latest completed review interval.
- MFE and MAE use observed stock-bar highs and lows after the alert.
- This report is retrospective decision support only and does not change live scanner rules.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a daily post-alert scanner performance review.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Trading date in YYYY-MM-DD format.")
    parser.add_argument("--log", default=str(APP_DIR / "logs" / "post_alert_performance.jsonl"))
    parser.add_argument("--output", default=None, help="Markdown report path.")
    args = parser.parse_args()
    output = Path(args.output).resolve() if args.output else APP_DIR / "exports" / f"alert_performance_{args.date}.md"
    records = read_latest_records(Path(args.log).resolve(), args.date)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_report(args.date, records), encoding="utf-8")
    print(f"Report: {output}")
    print(f"Alerts tracked: {len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
