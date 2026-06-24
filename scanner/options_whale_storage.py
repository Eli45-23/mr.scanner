from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def read_jsonl(path: Path, limit: int | None = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows[-limit:] if limit else rows


class OptionsWhaleStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.log_dir = root / "logs"
        self.alerts_path = self.log_dir / "options_whale_alerts.jsonl"
        self.qualified_events_path = self.log_dir / "options_whale_qualified_events.jsonl"
        self.scans_path = self.log_dir / "options_whale_scans.jsonl"
        self.oi_reviews_path = self.log_dir / "options_oi_reviews.jsonl"

    def append_alert(self, record: Dict[str, Any]) -> None:
        append_jsonl(self.alerts_path, record)

    def append_qualified_event(self, record: Dict[str, Any]) -> None:
        append_jsonl(self.qualified_events_path, record)

    def latest_qualified_events(self, limit: int = 1000) -> List[Dict[str, Any]]:
        return read_jsonl(self.qualified_events_path, limit=limit)

    def append_scan(self, record: Dict[str, Any]) -> None:
        append_jsonl(self.scans_path, record)

    def append_oi_review(self, record: Dict[str, Any]) -> None:
        append_jsonl(self.oi_reviews_path, record)

    def latest_alerts(self, limit: int = 100) -> List[Dict[str, Any]]:
        return read_jsonl(self.alerts_path, limit=limit)

    def latest_scans(self, limit: int = 20) -> List[Dict[str, Any]]:
        return read_jsonl(self.scans_path, limit=limit)

    def export_json(self, path: Path, records: Iterable[Dict[str, Any]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(records), indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, path: Path, records: Iterable[Dict[str, Any]]) -> Path:
        rows = list(records)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            path.write_text("", encoding="utf-8")
            return path
        fields = sorted({key for row in rows for key in row.keys()})
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in row.items()})
        return path
