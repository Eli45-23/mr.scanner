#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.options_whale_common import build_scanner, print_json


def main() -> int:
    scanner = build_scanner()
    print_json(scanner.rebuild_universe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
