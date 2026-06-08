#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import tempfile
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def unpack(path: Path, temp: Path) -> Path:
    if path.is_dir():
        return path
    target = temp / path.stem
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        archive.extractall(target)
    children = [item for item in target.iterdir() if item.is_dir()]
    return children[0] if len(children) == 1 else target


def read_jsonl(root: Path, name: str) -> list[dict[str, Any]]:
    matches = list(root.rglob(name))
    if not matches:
        return []
    rows = []
    for line in matches[0].read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def timestamp(row: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def scenario_name(row: dict[str, Any]) -> str:
    top = row.get("top_scenario") or row.get("scenario_top") or {}
    return str(top.get("scenario_name", "") if isinstance(top, dict) else top)


def alert_type(row: dict[str, Any]) -> str:
    return str(row.get("alert_source") or row.get("message_source_path") or row.get("alert_type") or row.get("heads_up_type") or "UNKNOWN")


def package_data(root: Path) -> dict[str, Any]:
    notifications = read_jsonl(root, "notification_status.jsonl")
    scenarios = read_jsonl(root, "scenario_engine.jsonl")
    heads = read_jsonl(root, "phase3_heads_up.jsonl")
    startups = read_jsonl(root, "scanner_startup_status.jsonl")
    market = read_jsonl(root, "market_data_status.jsonl")
    latest_start = startups[-1] if startups else {}
    latest_market = market[-1] if market else {}
    return {
        "notifications": notifications,
        "scenarios": scenarios,
        "heads": heads,
        "identity": latest_start,
        "market": latest_market,
        "counts": Counter(alert_type(row) for row in notifications),
    }


def compare_events(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in left:
        left_time = timestamp(item)
        matches = [
            other for other in right
            if left_time and timestamp(other) and abs((timestamp(other) - left_time).total_seconds()) <= 60
        ]
        match = matches[0] if matches else {}
        output.append({
            "timestamp": item.get("timestamp", ""),
            "symbol": item.get("symbol", ""),
            "left_type": alert_type(item),
            "right_type": alert_type(match) if match else "",
            "left_direction": item.get("direction", ""),
            "right_direction": match.get("direction", "") if match else "",
            "match_within_60s": bool(match),
        })
    return output


def compare_decisions(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in left:
        left_time = timestamp(item)
        matches = [
            other for other in right
            if left_time and timestamp(other) and abs((timestamp(other) - left_time).total_seconds()) <= 60
        ]
        if not matches:
            continue
        other = matches[0]
        fields = ("scenario_stage", "scenario_score", "stock_setup_score", "confirmation_score", "market_context", "scenario_alert_block_reason")
        differences = [field for field in fields if item.get(field) != other.get(field)]
        if scenario_name(item) != scenario_name(other):
            differences.insert(0, "top_scenario")
        if differences:
            output.append({
                "timestamp": item.get("timestamp", ""),
                "symbol": item.get("symbol", ""),
                "different_fields": differences,
                "left_scenario": scenario_name(item),
                "right_scenario": scenario_name(other),
            })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two redacted scanner review packages.")
    parser.add_argument("--left", required=True)
    parser.add_argument("--right", required=True)
    parser.add_argument("--left-name", default="Left")
    parser.add_argument("--right-name", default="Right")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as raw:
        temp = Path(raw)
        left = package_data(unpack(Path(args.left).resolve(), temp))
        right = package_data(unpack(Path(args.right).resolve(), temp))
    timeline = compare_events(left["notifications"], right["notifications"])
    decision_differences = compare_decisions(left["scenarios"], right["scenarios"])
    summary = {
        "left_name": args.left_name,
        "right_name": args.right_name,
        "left_identity": left["identity"],
        "right_identity": right["identity"],
        "left_market": left["market"],
        "right_market": right["market"],
        "left_alert_counts": dict(left["counts"]),
        "right_alert_counts": dict(right["counts"]),
        "timeline": timeline,
        "decision_differences": decision_differences,
    }
    json_path = output.with_suffix(".json")
    csv_path = output.with_suffix(".csv")
    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(timeline[0]) if timeline else ["timestamp"])
        writer.writeheader()
        writer.writerows(timeline)
    identity_keys = ["scanner_instance_name", "hostname", "git_commit", "scanner_alert_profile", "alert_types_enabled", "alert_symbols", "context_symbols", "telegram_destination_type", "telegram_chat_id_last4"]
    lines = [f"# Scanner Comparison: {args.left_name} vs {args.right_name}", "", "## Code and Config", "", "| Field | Left | Right |", "| --- | --- | --- |"]
    for key in identity_keys:
        lines.append(f"| {key} | {left['identity'].get(key, 'unavailable')} | {right['identity'].get(key, 'unavailable')} |")
    lines += ["", "## Alert Counts", "", "| Alert Type | Left | Right |", "| --- | ---: | ---: |"]
    for key in sorted(set(left["counts"]) | set(right["counts"])):
        lines.append(f"| {key} | {left['counts'].get(key, 0)} | {right['counts'].get(key, 0)} |")
    lines += ["", "## Timeline and Decisions", "", f"- Left events: {len(left['notifications'])}", f"- Right events: {len(right['notifications'])}", f"- Left events matched within 60 seconds: {sum(1 for row in timeline if row['match_within_60s'])}", f"- Scenario decision differences within 60 seconds: {len(decision_differences)}", "", f"JSON: `{json_path}`", f"CSV: `{csv_path}`", ""]
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown: {output}")
    print(f"JSON: {json_path}")
    print(f"CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
