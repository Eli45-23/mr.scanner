#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.dashboard_snapshot_exporter import EXPORT_DIR, DEFAULT_DASHBOARD_URL, DEFAULT_LOG_DIR, export_dashboard_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the live dashboard state for ChatGPT review.")
    parser.add_argument("--base-url", default=DEFAULT_DASHBOARD_URL, help="Dashboard base URL.")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="Path to the scanner log directory.")
    parser.add_argument("--output-dir", default=str(EXPORT_DIR), help="Directory for export files.")
    args = parser.parse_args()

    result = export_dashboard_snapshot(
        base_url=args.base_url,
        log_dir=Path(args.log_dir),
        output_dir=Path(args.output_dir),
    )
    print(json.dumps({"json": str(result["paths"]["json"]), "md": str(result["paths"]["md"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
