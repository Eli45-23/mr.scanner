#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
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


def _episode_time(row: Dict[str, Any]) -> datetime | None:
    candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else row
    raw = row.get("scanner_detected_time") or candidate.get("time_detected") or row.get("timestamp") or row.get("episode_updated_at")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def prior_session_episodes(rows: Iterable[Dict[str, Any]], *, as_of: date) -> tuple[str | None, List[Dict[str, Any]]]:
    dated = [(stamp.date(), row) for row in rows if (stamp := _episode_time(row)) and stamp.date() < as_of]
    source_day = max((day for day, _ in dated), default=None)
    return (source_day.isoformat() if source_day else None, [row for day, row in dated if day == source_day])


def build_client(config: Dict[str, Any]) -> OptionsDataClient:
    return OptionsDataClient(
        stock_feed=str(config.get("market_data", {}).get("stock_feed", "sip")),
        options_feed=str(config.get("options", {}).get("feed", "opra")),
        allow_indicative_fallback=bool(config.get("options", {}).get("allow_indicative_fallback", True)),
    )


def append_reviews(rows: Iterable[Dict[str, Any]], storage: OptionsWhaleStorage) -> None:
    existing = {(str(row.get("episode_id") or ""), str(row.get("option_symbol") or ""), str(row.get("original_time") or "")): row for row in storage.latest_oi_reviews(limit=20000)}
    for row in rows:
        key = (str(row.get("episode_id") or ""), str(row.get("option_symbol") or ""), str(row.get("original_time") or ""))
        prior = existing.get(key)
        if not prior or (str(prior.get("next_day_oi_status")) in {"pending", "unavailable", "unresolved"} and str(row.get("next_day_oi_status")) not in {"pending", "unavailable", "unresolved"}):
            storage.append_oi_review({"reviewed_at": datetime.now(timezone.utc).isoformat(), **row})
            existing[key] = row


def review_from_live_contracts(*, limit: int = 100, dry_run: bool = False, latest_only: bool = False, source_date: str | None = None, as_of: date | None = None) -> Dict[str, Any]:
    scanner_app.load_dotenv()
    config = scanner_app.load_config(None)
    storage = OptionsWhaleStorage(ROOT)
    all_rows = _read_latest_results() if latest_only else storage.latest_episodes(limit=max(20000, limit)) or storage.latest_alerts(limit=max(20000, limit)) or _read_latest_results()
    if source_date:
        source_day = source_date
        alerts = [row for row in all_rows if (_episode_time(row) and _episode_time(row).date().isoformat() == source_date)]
    else:
        source_day, alerts = prior_session_episodes(all_rows, as_of=as_of or datetime.now(timezone.utc).date())
    alerts = _unique_by_contract(alerts)
    oi_map = fetch_next_day_oi_map(build_client(config), alerts)
    reviews = review_alerts_with_next_day_oi(alerts, oi_map)
    reviewed_symbols = {str(row.get("option_symbol") or "") for row in reviews}
    unresolved = []
    for alert in alerts:
        candidate = alert.get("candidate") if isinstance(alert.get("candidate"), dict) else alert
        symbol = str(candidate.get("option_symbol") or candidate.get("contract_symbol") or "")
        if symbol and symbol not in reviewed_symbols:
            unresolved.append({"option_symbol": symbol, "underlying_symbol": candidate.get("underlying_symbol"), "expiration": candidate.get("expiration"), "option_type": candidate.get("option_type"), "strike": candidate.get("strike"), "original_time": alert.get("scanner_detected_time") or candidate.get("time_detected") or alert.get("timestamp"), "episode_id": alert.get("episode_id") or alert.get("flow_episode_id"), "next_day_oi_status": "unavailable", "open_close_estimate_after_oi": "unresolved", "next_day_oi_reason": "Next-day OI was unavailable; do not infer opening, closing, rolling, or hedging intent."})
    reviews.extend(unresolved)
    if reviews and not dry_run:
        append_reviews(reviews, storage)
    statuses: Dict[str, int] = {}
    for row in reviews:
        status = str(row.get("next_day_oi_status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "mode": "live_contracts",
        "alerts_checked": len(alerts),
        "source_session_date": source_day,
        "unique_contract_count": len(alerts),
        "oi_values_found": len(oi_map),
        "oi_coverage_rate": round(len(oi_map) / len(alerts), 4) if alerts else None,
        "reviewed_count": len(reviews),
        "unresolved_count": len(unresolved),
        "complete": len(oi_map) > 0,
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
    parser.add_argument("--source-date", help="Review contracts detected on this source session date (YYYY-MM-DD).")
    args = parser.parse_args()
    if args.oi_json:
        result = review_from_oi_json(Path(args.oi_json))
    else:
        result = review_from_live_contracts(limit=args.limit, dry_run=args.dry_run, latest_only=args.latest_only, source_date=args.source_date)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
