#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import elite_momentum_scanner as scanner_app
from scanner.options_alert_outcomes import evaluate_alert_outcome, summarize_outcomes
from scanner.options_data_client import OptionsDataClient
from tools.summarize_options_outcomes import is_clean_completed


APP_DIR = Path(__file__).resolve().parents[1]
LATEST_PATH = APP_DIR / "data" / "options_whale_latest.json"
OUTCOMES_PATH = APP_DIR / "data" / "options_whale_outcomes.jsonl"
FINAL_OUTCOME_STATUSES = {"ok"}


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_latest(path: Path = LATEST_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def candidate(row: Dict[str, Any]) -> Dict[str, Any]:
    return row.get("candidate") if isinstance(row.get("candidate"), dict) else row


def alert_key(row: Dict[str, Any]) -> str:
    c = candidate(row)
    return "|".join(str(part or "") for part in (
        row.get("timestamp") or row.get("time_detected") or c.get("time_detected"),
        c.get("underlying_symbol"),
        c.get("option_symbol"),
        row.get("whale_score") or row.get("score"),
    ))


def load_finalized_keys(path: Path = OUTCOMES_PATH) -> set[str]:
    if not path.exists():
        return set()
    latest_by_key: Dict[str, Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = row.get("alert_key")
        if key:
            latest_by_key[str(key)] = row
    return {
        key
        for key, row in latest_by_key.items()
        if str(row.get("outcome_status") or "") in FINAL_OUTCOME_STATUSES and is_clean_completed(row)
    }


def append_outcomes(rows: Iterable[Dict[str, Any]], path: Path = OUTCOMES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def build_client(config: Dict[str, Any]) -> OptionsDataClient:
    return OptionsDataClient(
        stock_feed=str(config.get("market_data", {}).get("stock_feed", "sip")),
        options_feed=str(config.get("options", {}).get("feed", "opra")),
        allow_indicative_fallback=bool(config.get("options", {}).get("allow_indicative_fallback", True)),
    )


def review_alerts(limit: int = 25, *, include_near_misses: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    scanner_app.load_dotenv()
    config = scanner_app.load_config(None)
    client = build_client(config)
    latest = read_latest()
    results = list(latest.get("results") or [])
    if include_near_misses:
        results.extend(latest.get("near_misses") or [])
    results = results[: max(1, int(limit))]
    finalized = load_finalized_keys()
    reviewed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in results:
        c = candidate(row)
        key = alert_key(row)
        if key in finalized:
            skipped.append({"alert_key": key, "reason": "already_finalized"})
            continue
        symbol = str(c.get("underlying_symbol") or c.get("underlying") or "").upper()
        detected_at = parse_time(row.get("timestamp") or c.get("time_detected") or latest.get("timestamp"))
        if not symbol or detected_at is None:
            skipped.append({"alert_key": key, "reason": "missing_symbol_or_time"})
            continue
        start = detected_at - timedelta(minutes=1)
        end = detected_at + timedelta(minutes=70)
        try:
            bars = client.get_stock_bars([symbol], start=start, end=end).get(symbol, [])
        except Exception as exc:
            skipped.append({"alert_key": key, "reason": f"bars_unavailable: {exc}"})
            continue
        outcome = evaluate_alert_outcome(row, bars)
        reviewed.append({
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "alert_key": key,
            "underlying_symbol": symbol,
            "option_symbol": c.get("option_symbol"),
            "option_type": c.get("option_type"),
            "strike": c.get("strike"),
            "expiration": c.get("expiration"),
            "whale_score": row.get("whale_score") or row.get("score"),
            "classification": row.get("classification"),
            "alert_tier": row.get("alert_tier"),
            "score_components": row.get("score_components"),
            **outcome,
        })

    if reviewed and not dry_run:
        append_outcomes(reviewed)
    return {
        "reviewed_count": len(reviewed),
        "skipped_count": len(skipped),
        "output_path": str(OUTCOMES_PATH.relative_to(APP_DIR)),
        "dry_run": dry_run,
        "summary": summarize_outcomes(reviewed),
        "skipped": skipped[:10],
        "reviewed": reviewed[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Review latest options whale alert outcomes using underlying stock bars.")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--include-near-misses", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(review_alerts(args.limit, include_near_misses=args.include_near_misses, dry_run=args.dry_run), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
