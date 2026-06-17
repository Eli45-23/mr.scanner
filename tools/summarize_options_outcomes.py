#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


APP_DIR = Path(__file__).resolve().parents[1]
OUTCOMES_PATH = APP_DIR / "data" / "options_whale_outcomes.jsonl"
COMPLETED_STATUSES = {"ok", "partial"}


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def score_bucket(score: Any) -> str:
    value = safe_float(score)
    if value is None:
        return "unknown"
    if value >= 90:
        return "90-100"
    if value >= 80:
        return "80-89"
    if value >= 70:
        return "70-79"
    if value >= 60:
        return "60-69"
    return "below-60"


def unusualness_bucket(row: Dict[str, Any]) -> str:
    components = row.get("score_components") if isinstance(row.get("score_components"), dict) else {}
    value = safe_float(components.get("historical_unusualness"))
    if value is None:
        return "unknown"
    if value >= 12:
        return "12+ extreme"
    if value >= 8:
        return "8-11 high"
    if value >= 4:
        return "4-7 moderate"
    return "0-3 low"


def load_outcomes(path: Path = OUTCOMES_PATH) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def latest_by_alert_key(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    anonymous_index = 0
    for row in rows:
        key = str(row.get("alert_key") or "")
        if not key:
            anonymous_index += 1
            key = f"anonymous-{anonymous_index}"
        latest[key] = row
    return list(latest.values())


def valid_completed_window_count(row: Dict[str, Any]) -> int:
    windows = row.get("windows") or []
    return sum(
        1
        for item in windows
        if isinstance(item, dict)
        and item.get("status") == "ok"
        and safe_float(item.get("move_pct")) is not None
    )


def is_clean_completed(row: Dict[str, Any]) -> bool:
    return str(row.get("outcome_status")) in COMPLETED_STATUSES and valid_completed_window_count(row) > 0


def is_dirty_completed(row: Dict[str, Any]) -> bool:
    return str(row.get("outcome_status")) in COMPLETED_STATUSES and not is_clean_completed(row)


def is_favorable(row: Dict[str, Any]) -> bool:
    windows = row.get("windows") or []
    return any(isinstance(item, dict) and item.get("favorable") is True and safe_float(item.get("move_pct")) is not None for item in windows)


def group_key(row: Dict[str, Any], group_by: str) -> str:
    if group_by == "symbol":
        return str(row.get("underlying_symbol") or "UNKNOWN").upper()
    if group_by == "option_type":
        return str(row.get("option_type") or "UNKNOWN").upper()
    if group_by == "flow_bias":
        return str(row.get("flow_bias") or "UNKNOWN").upper()
    if group_by == "flow_bias_source":
        return str(row.get("flow_bias_source") or "UNKNOWN")
    if group_by == "alert_tier":
        return str(row.get("alert_tier") or "UNKNOWN")
    if group_by == "score_bucket":
        return score_bucket(row.get("whale_score"))
    if group_by == "unusualness_bucket":
        return unusualness_bucket(row)
    if group_by == "symbol_flow_bias":
        return f"{str(row.get('underlying_symbol') or 'UNKNOWN').upper()}|{str(row.get('flow_bias') or 'UNKNOWN').upper()}"
    return "ALL"


def summarize_group(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = list(rows)
    completed = [row for row in items if is_clean_completed(row)]
    pending = [row for row in items if str(row.get("outcome_status")) == "pending" or is_dirty_completed(row)]
    insufficient = [row for row in items if str(row.get("outcome_status")) == "insufficient_future_session"]
    dirty = [row for row in items if is_dirty_completed(row)]
    missing = [row for row in items if str(row.get("outcome_status")) == "missing_start_context"]
    favorable = [row for row in completed if is_favorable(row)]
    max_favorable_values = [
        value for value in (safe_float(row.get("max_favorable_move_pct")) for row in completed) if value is not None
    ]
    max_adverse_values = [
        value for value in (safe_float(row.get("max_adverse_move_pct")) for row in completed) if value is not None
    ]
    scores = [value for value in (safe_float(row.get("whale_score")) for row in items) if value is not None]
    return {
        "count": len(items),
        "completed": len(completed),
        "pending": len(pending),
        "insufficient_future_session": len(insufficient),
        "dirty_completed_ignored": len(dirty),
        "missing_start_context": len(missing),
        "favorable_count": len(favorable),
        "favorable_rate": round(len(favorable) / len(completed), 4) if completed else None,
        "average_max_favorable_move_pct": round(sum(max_favorable_values) / len(max_favorable_values), 4) if max_favorable_values else None,
        "average_max_adverse_move_pct": round(sum(max_adverse_values) / len(max_adverse_values), 4) if max_adverse_values else None,
        "average_whale_score": round(sum(scores) / len(scores), 2) if scores else None,
    }


def summarize_outcome_file(path: Path = OUTCOMES_PATH, *, min_completed: int = 1) -> Dict[str, Any]:
    raw_rows = load_outcomes(path)
    rows = latest_by_alert_key(raw_rows)
    report: Dict[str, Any] = {
        "source_path": str(path.relative_to(APP_DIR)) if path.is_absolute() and APP_DIR in path.parents else str(path),
        "raw_record_count": len(raw_rows),
        "unique_alert_count": len(rows),
        "overall": summarize_group(rows),
        "groups": {},
    }
    for name in (
        "symbol",
        "symbol_flow_bias",
        "option_type",
        "flow_bias",
        "flow_bias_source",
        "alert_tier",
        "score_bucket",
        "unusualness_bucket",
    ):
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[group_key(row, name)].append(row)
        summaries: List[Tuple[str, Dict[str, Any]]] = []
        for key, group_rows in buckets.items():
            summary = summarize_group(group_rows)
            if int(summary["completed"]) >= min_completed or int(summary["pending"]) > 0 or int(summary["insufficient_future_session"]) > 0:
                summaries.append((key, summary))
        summaries.sort(
            key=lambda item: (
                item[1].get("completed") or 0,
                item[1].get("favorable_rate") if item[1].get("favorable_rate") is not None else -1,
                item[1].get("count") or 0,
            ),
            reverse=True,
        )
        report["groups"][name] = [{"key": key, **summary} for key, summary in summaries]
    return report


def compact_table(report: Dict[str, Any], group: str, limit: int = 12) -> str:
    rows = report.get("groups", {}).get(group, [])[:limit]
    if not rows:
        return f"No rows for {group}."
    lines = [f"{group} performance", "key | count | completed | pending | insufficient | dirty_ignored | favorable_rate | avg_fav_move | avg_score"]
    for row in rows:
        rate = row.get("favorable_rate")
        rate_text = "pending" if rate is None else f"{rate * 100:.1f}%"
        fav = row.get("average_max_favorable_move_pct")
        fav_text = "" if fav is None else f"{fav:+.4f}%"
        lines.append(
            f"{row['key']} | {row['count']} | {row['completed']} | {row['pending']} | {row.get('insufficient_future_session', 0)} | {row.get('dirty_completed_ignored', 0)} | {rate_text} | {fav_text} | {row.get('average_whale_score')}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize options whale outcome history.")
    parser.add_argument("--path", default=str(OUTCOMES_PATH), help="Path to options outcome JSONL file.")
    parser.add_argument("--group", default="symbol_flow_bias", choices=[
        "symbol", "symbol_flow_bias", "option_type", "flow_bias", "flow_bias_source", "alert_tier", "score_bucket", "unusualness_bucket"
    ])
    parser.add_argument("--min-completed", type=int, default=1)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args()
    report = summarize_outcome_file(Path(args.path), min_completed=max(0, args.min_completed))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        overall = report["overall"]
        print("Options Whale Outcome Summary")
        print(f"raw_records: {report['raw_record_count']} | unique_alerts: {report['unique_alert_count']}")
        print(
            f"overall: count={overall['count']} completed={overall['completed']} pending={overall['pending']} "
            f"insufficient={overall.get('insufficient_future_session', 0)} dirty_ignored={overall.get('dirty_completed_ignored', 0)} "
            f"favorable_rate={overall['favorable_rate']} avg_fav_move={overall['average_max_favorable_move_pct']}"
        )
        print()
        print(compact_table(report, args.group, limit=max(1, args.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
