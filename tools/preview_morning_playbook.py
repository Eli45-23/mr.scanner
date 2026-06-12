#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scanner.morning_playbook import (
    build_morning_playbook_payload,
    format_morning_playbook_message,
    validate_morning_playbook_message,
)


def sample_payload() -> dict:
    return build_morning_playbook_payload(
        "AAPL",
        292.0,
        {"pmh": 292.4, "pml": 289.8, "pdh": 294.1, "pdl": 289.25, "pdc": 291.6},
        market_structure={
            "market_structure_bias": "MIXED",
            "structure_quality": "HIGH",
            "structure_warning": "inside chop range",
            "chop_range_detected": True,
            "current_price_location_summary": "AAPL is between 5m demand and 5m supply.",
            "major_support_area": {"price": 290.1},
            "major_resistance_area": {"price": 292.4},
            "major_demand_area": {
                "zone_low": 290.0,
                "zone_high": 290.4,
                "precision_zone_low": 290.18,
                "precision_zone_high": 290.28,
                "quality_label": "A Zone",
                "trigger_level": 290.22,
            },
            "major_supply_area": {
                "zone_low": 292.2,
                "zone_high": 292.6,
                "precision_zone_low": 292.36,
                "precision_zone_high": 292.45,
                "quality_label": "A Zone",
                "trigger_level": 292.4,
            },
        },
        liquidity_sweep={
            "nearest_upside_sweep_zone": {"level": 292.4},
            "nearest_downside_sweep_zone": {"level": 290.05},
            "sweep_status": "SWEEP_WATCH",
            "trap_bias": "NEUTRAL",
            "confidence": "MEDIUM",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview the safe AAPL Morning Playbook without market data or Telegram.")
    parser.add_argument("--json", action="store_true", help="Print the sample payload as JSON.")
    parser.add_argument("--max-chars", type=int, default=1200)
    args = parser.parse_args()
    payload = sample_payload()
    message = format_morning_playbook_message(payload, max_chars=args.max_chars)
    valid, reason = validate_morning_playbook_message(payload, message, max_chars=args.max_chars)
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else message)
    print(f"\nValidation: {'PASS' if valid else 'FAIL'}{'' if valid else f' - {reason}'}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
