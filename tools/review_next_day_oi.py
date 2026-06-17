#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner_app
from scanner.options_data_client import OptionsDataClient
from scanner.options_oi_review import fetch_next_day_oi_map, review_alerts_with_next_day_oi
from scanner.options_whale_storage import OptionsWhaleStorage

LATEST_PATH = ROOT / "data" / "options_whale_latest.json"


def _read_latest_results(path: Path = LATEST_PATH) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    return [row for row in data.get("results") or [] if isinstance(row, dict)]


def _unique_by_contract(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else row
        key = candidate.get("option_symbol") or candidate.get("contract_symbol")
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def build_client(config: Dict[str, Any]) -> OptionsDataClient:
    return OptionsDataClient(
        stock_feed=str(config.get("market_data", {}).get("stock_feed", "sip")),
        options_feed=str(config.get("options", {}).get("feed", "opra")),
        allow_indicative_fallback=bool(config.get("options", {}).get("allow_indicative_fallback", True)),
    )


def append_reviews(rows: Iterable[Dict[str, Any]], storage: OptionsWhaleStorage) -> None:
    for row in rows:
        storage.append_oi_review({"reviewed_at": datetime.now(timezone.utc).isoformat(), **row})


def review_from_live_contracts(*, limit: int = 100, dry_run: bool = False, latest_only: bool = False) -> Dict[str, Any]:
    scanner_app.load_dotenv()
    config = scanner_app.load_config(None)
    storage = OptionsWhaleStorage(ROOT)
    alerts = _read_latest_results() if latest_only else storage.latest_alerts(limit=limit) or _read_latest_results()
    alerts = _unique_by_contract(alerts)[-max(1, int(limit)):]
    oi_map = fetch_next_day_oi_map(build_client(config), alerts)
    reviews = review_alerts_with_next_day_oi(alerts, oi_map)
    if reviews and not dry_run:
        append_reviews(reviews, storage)
    statuses: Dict[str, int] = {}
    for row in reviews:
        status = str(row.get("next_day_oi_status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "mode": "live_contracts",
        "alerts_checked": len(alerts),
        "oi_values_found": len(oi_map),
        "reviewed_count": len(reviews),
        "dry_run": dry_run,
        "output_path": str(storage.oi_reviews_path.relative_to(ROOT)),
        "statuses": statuses,
        "reviews": reviews[:20],
    }


def review_from_oi_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("OI JSON must be an object mapping option symbols to open interest.")
    storage = OptionsWhaleStorage(ROOT)
    alerts = storage.latest_alerts(limit=500) or _read_latest_results()
    reviews = review_alerts_with_next_day_oi(alerts, {str(k): int(v) for k, v in payload.items()})
    return {"mode": "oi_json", "reviewed_count": len(reviews), "reviews": reviews}


def main() -> int:
    parser = argparse.ArgumentParser(description="Review whale-flow alerts against next-day open interest.")
    parser.add_argument("--oi-json", help="Optional JSON object mapping option symbols to next-day open interest.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--latest-only", action="store_true", help="Use only data/options_whale_latest.json results instead of alert history.")
    args = parser.parse_args()
    if args.oi_json:
        result = review_from_oi_json(Path(args.oi_json))
    else:
        result = review_from_live_contracts(limit=args.limit, dry_run=args.dry_run, latest_only=args.latest_only)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
