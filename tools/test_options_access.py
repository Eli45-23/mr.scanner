#!/usr/bin/env python3
from __future__ import annotations

from tools.options_whale_common import build_scanner, print_json


def main() -> int:
    scanner = build_scanner()
    print_json(scanner.status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
