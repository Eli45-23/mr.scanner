#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.options_whale_common import build_scanner, print_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Review prior whale-flow alerts against next-day open interest.")
    parser.add_argument("--oi-json", required=True, help="JSON object mapping option symbols to next-day open interest.")
    args = parser.parse_args()
    payload = json.loads(Path(args.oi_json).read_text(encoding="utf-8"))
    scanner = build_scanner()
    print_json({"reviews": scanner.review_next_day_oi({str(k): int(v) for k, v in payload.items()})})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
