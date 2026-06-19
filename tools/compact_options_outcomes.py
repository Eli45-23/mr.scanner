#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from tools.summarize_options_outcomes import OUTCOMES_PATH, is_dirty_completed, latest_by_alert_key, load_outcomes


DEFAULT_OUTPUT_PATH = OUTCOMES_PATH.with_name("options_whale_outcomes.compacted.jsonl")


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def compact_outcomes(path: Path = OUTCOMES_PATH) -> Dict[str, Any]:
    raw_rows = load_outcomes(path)
    latest_rows = latest_by_alert_key(raw_rows)
    clean_rows = [row for row in latest_rows if not is_dirty_completed(row)]
    return {
        "raw_count": len(raw_rows),
        "unique_count": len(latest_rows),
        "clean_count": len(clean_rows),
        "duplicate_count": len(raw_rows) - len(latest_rows),
        "dirty_completed_removed": len(latest_rows) - len(clean_rows),
        "rows": clean_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a compact latest-by-alert-key options outcome file.")
    parser.add_argument("--path", default=str(OUTCOMES_PATH), help="Source options outcome JSONL file.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output JSONL path when not using --in-place.")
    parser.add_argument("--in-place", action="store_true", help="Rewrite the source path instead of writing a separate compacted file.")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without writing any file.")
    args = parser.parse_args()

    source = Path(args.path)
    output = source if args.in_place else Path(args.output)
    result = compact_outcomes(source)
    if not args.dry_run:
        write_jsonl(output, result["rows"])
    print(json.dumps({
        "source_path": str(source),
        "output_path": str(output),
        "dry_run": bool(args.dry_run),
        "in_place": bool(args.in_place),
        "raw_count": result["raw_count"],
        "unique_count": result["unique_count"],
        "clean_count": result["clean_count"],
        "duplicate_count": result["duplicate_count"],
        "dirty_completed_removed": result["dirty_completed_removed"],
        "wrote_output": not args.dry_run,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
