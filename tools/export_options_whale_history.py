#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.options_whale_common import build_scanner, print_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Options Whale Scanner history.")
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    scanner = build_scanner()
    records = scanner.storage.latest_alerts(limit=10000)
    if args.output:
        path = Path(args.output)
    else:
        path = Path("exports") / f"options_whale_history.{args.format}"
    if args.format == "json":
        scanner.storage.export_json(path, records)
    else:
        scanner.storage.export_csv(path, records)
    print_json({"export_path": str(path), "record_count": len(records)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
