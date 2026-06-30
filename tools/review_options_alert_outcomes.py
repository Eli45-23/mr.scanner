#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import elite_momentum_scanner as scanner_app
from scanner.options_alert_outcomes import evaluate_alert_outcome, evaluate_option_price_outcome, summarize_outcomes
from scanner.options_data_client import OptionsDataClient
from tools.summarize_options_outcomes import is_clean_completed
from scanner.options_whale_storage import OptionsWhaleStorage


LATEST_PATH = APP_DIR / "data" / "options_whale_latest.json"
OUTCOMES_PATH = APP_DIR / "data" / "options_whale_outcomes.jsonl"
FINAL_OUTCOME_STATUSES = {"ok"}
OUTCOME_WINDOWS = (5, 15, 30, 60)


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


def read_latest(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or LATEST_PATH
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
    episode_id = row.get("flow_episode_id") or row.get("episode_id")
    if episode_id:
        return f"episode|{episode_id}"
    c = candidate(row)
    return "|".join(str(part or "") for part in (
        row.get("timestamp") or row.get("time_detected") or c.get("time_detected"),
        c.get("underlying_symbol"),
        c.get("option_symbol"),
        row.get("whale_score") or row.get("score"),
    ))


def load_finalized_keys(path: Optional[Path] = None) -> set[str]:
    latest_by_key = load_latest_outcomes_by_key(path)
    return {
        key
        for key, row in latest_by_key.items()
        if str(row.get("outcome_status") or "") in FINAL_OUTCOME_STATUSES and is_clean_completed(row)
    }


def load_outcome_rows(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or OUTCOMES_PATH
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_latest_outcomes_by_key(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    latest_by_key: Dict[str, Dict[str, Any]] = {}
    for row in load_outcome_rows(path):
        key = row.get("alert_key")
        if key:
            latest_by_key[str(key)] = row
    return latest_by_key


def completed_window_count(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("completed_window_count") or 0)
    except (TypeError, ValueError):
        return 0


def should_append_outcome(new_row: Dict[str, Any], previous: Optional[Dict[str, Any]], *, force: bool = False) -> bool:
    if force or previous is None:
        return True
    if completed_window_count(new_row) > completed_window_count(previous):
        return True
    previous_status = str(previous.get("outcome_status") or "")
    new_status = str(new_row.get("outcome_status") or "")
    if previous_status == "pending" and new_status not in {"pending", ""}:
        return True
    return False


def append_outcomes(rows: Iterable[Dict[str, Any]], path: Optional[Path] = None) -> None:
    path = path or OUTCOMES_PATH
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


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(APP_DIR))
    except ValueError:
        return str(path)


def _bar_timestamp(bar: Dict[str, Any]) -> Optional[datetime]:
    return parse_time(bar.get("t") or bar.get("timestamp") or bar.get("time"))


def _window_targets(detected_at: datetime, windows: Iterable[int]) -> List[datetime]:
    return [detected_at.replace(microsecond=0) + timedelta(minutes=int(minutes)) for minutes in windows]


def outcome_debug_reason(
    *,
    bars: List[Dict[str, Any]],
    detected_at: Optional[datetime],
    outcome: Dict[str, Any],
    windows: Iterable[int] = OUTCOME_WINDOWS,
) -> str:
    if detected_at is None:
        return "alert timestamp could not be parsed"
    if outcome.get("outcome_status") == "missing_start_context":
        return "base price missing"
    if not bars:
        return "no bars returned"
    if int(outcome.get("pending_window_count") or 0) <= 0:
        return ""
    bar_times = [stamp for stamp in (_bar_timestamp(bar) for bar in bars) if stamp is not None]
    if not bar_times:
        return "bars returned but timestamps could not be parsed"
    latest_bar = max(bar_times)
    targets = _window_targets(detected_at, windows)
    if targets and all(target > latest_bar for target in targets):
        return "all target windows are after latest returned bar"
    return "bars returned but no bars at or after target windows"


def build_outcome_diagnostics(
    *,
    bars: List[Dict[str, Any]],
    start: datetime,
    end: datetime,
    detected_at: Optional[datetime],
    outcome: Dict[str, Any],
    windows: Iterable[int] = OUTCOME_WINDOWS,
) -> Dict[str, Any]:
    bar_times = [stamp for stamp in (_bar_timestamp(bar) for bar in bars) if stamp is not None]
    diagnostics = {
        "bars_returned": len(bars),
        "bars_start_requested": start.isoformat(),
        "bars_end_requested": end.isoformat(),
        "first_bar_time": min(bar_times).isoformat() if bar_times else None,
        "last_bar_time": max(bar_times).isoformat() if bar_times else None,
        "detected_at": detected_at.isoformat() if detected_at else None,
        "outcome_window_minutes_requested": list(windows),
    }
    reason = outcome_debug_reason(bars=bars, detected_at=detected_at, outcome=outcome, windows=windows)
    if reason:
        diagnostics["outcome_debug_reason"] = reason
    return diagnostics


def review_alerts(
    limit: int = 25,
    *,
    include_near_misses: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    scanner_app.load_dotenv()
    config = scanner_app.load_config(None)
    client = build_client(config)
    now_utc = datetime.now(timezone.utc)
    latest = read_latest()
    storage_root = LATEST_PATH.parent.parent if LATEST_PATH.parent.name == "data" else LATEST_PATH.parent
    storage = OptionsWhaleStorage(storage_root)
    episodes = storage.latest_episodes(limit=max(1, int(limit)))
    episode_mode = bool(episodes)
    results = episodes or storage.latest_qualified_events(limit=max(1, int(limit))) or list(latest.get("results") or [])
    outcome_path = storage.episode_outcomes_path if episode_mode else OUTCOMES_PATH
    if include_near_misses:
        results.extend(latest.get("near_misses") or [])
    results = results[: max(1, int(limit))]
    finalized = load_finalized_keys(outcome_path)
    previous_by_key = load_latest_outcomes_by_key(outcome_path)
    reviewed: List[Dict[str, Any]] = []
    appendable: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    unchanged_pending_count = 0

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
        start = detected_at - timedelta(minutes=10)
        target_end = detected_at + timedelta(minutes=75)
        end = now_utc if target_end > now_utc else max(now_utc, target_end)
        try:
            bars = client.get_stock_bars([symbol], start=start, end=end).get(symbol, [])
        except Exception as exc:
            skipped.append({"alert_key": key, "reason": f"bars_unavailable: {exc}"})
            continue
        outcome = evaluate_alert_outcome(row, bars, windows=OUTCOME_WINDOWS)
        option_symbol = str(c.get("option_symbol") or "")
        option_bars: List[Dict[str, Any]] = []
        option_quotes: List[Dict[str, Any]] = []
        if option_symbol and hasattr(client, "get_option_bars"):
            try:
                option_bars = client.get_option_bars([option_symbol], start=start, end=end).get(option_symbol, [])
            except Exception:
                option_bars = []
        if option_symbol and hasattr(client, "get_option_quotes"):
            try:
                option_quotes = client.get_option_quotes([option_symbol], start=start, end=end).get(option_symbol, [])
            except Exception:
                option_quotes = []
        option_outcome = evaluate_option_price_outcome(row, option_bars, option_quotes, windows=OUTCOME_WINDOWS)
        reviewed_row = {
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "alert_key": key,
            "episode_id": row.get("flow_episode_id") or row.get("episode_id"),
            "underlying_symbol": symbol,
            "option_symbol": c.get("option_symbol"),
            "option_type": c.get("option_type"),
            "strike": c.get("strike"),
            "expiration": c.get("expiration"),
            "whale_score": row.get("whale_score") or row.get("score"),
            "dte": c.get("dte"),
            "dte_bucket": c.get("dte_bucket"),
            "direction_confidence": row.get("direction_confidence") or c.get("direction_confidence"),
            "market_regime": row.get("market_regime") or c.get("market_regime") or "UNKNOWN",
            "classification": row.get("classification"),
            "alert_tier": row.get("alert_tier"),
            "score_components": row.get("score_components"),
            **outcome,
            **option_outcome,
            **build_outcome_diagnostics(
                bars=bars,
                start=start,
                end=end,
                detected_at=detected_at,
                outcome=outcome,
                windows=OUTCOME_WINDOWS,
            ),
        }
        reviewed.append(reviewed_row)
        if should_append_outcome(reviewed_row, previous_by_key.get(key), force=force):
            appendable.append(reviewed_row)
        elif str(reviewed_row.get("outcome_status") or "") == "pending":
            unchanged_pending_count += 1

    if appendable and not dry_run:
        append_outcomes(appendable, outcome_path)
    summary = summarize_outcomes(reviewed)
    return {
        "reviewed_count": len(reviewed),
        "skipped_count": len(skipped),
        "appended_count": len(appendable) if not dry_run else 0,
        "appendable_count": len(appendable),
        "unchanged_pending_count": unchanged_pending_count,
        "completed_count": summary.get("completed", 0),
        "pending_count": summary.get("pending", 0),
        "insufficient_future_session_count": summary.get("insufficient_future_session", 0),
        "output_path": display_path(outcome_path),
        "episode_mode": episode_mode,
        "dry_run": dry_run,
        "force": force,
        "summary": summary,
        "skipped": skipped[:10],
        "reviewed": reviewed[:10],
    }


def print_review_result(result: Dict[str, Any], *, compact: bool = False) -> None:
    if compact:
        summary = result.get("summary") or {}
        print(
            f"{datetime.now().astimezone().isoformat(timespec='seconds')} | "
            f"reviewed={result.get('reviewed_count')} skipped={result.get('skipped_count')} "
            f"appended={result.get('appended_count')} unchanged_pending={result.get('unchanged_pending_count')} "
            f"completed={summary.get('completed')} pending={summary.get('pending')} "
            f"favorable_rate={summary.get('favorable_rate')}"
        )
    else:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))


def run_loop(limit: int, *, include_near_misses: bool, dry_run: bool, force: bool, interval_seconds: int, once: bool = False) -> int:
    interval = max(60, int(interval_seconds))
    while True:
        result = review_alerts(limit, include_near_misses=include_near_misses, dry_run=dry_run, force=force)
        print_review_result(result, compact=True)
        if once:
            return 0
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Review latest options whale alert outcomes using underlying stock bars.")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--include-near-misses", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Append unchanged rows, including duplicate pending reviews.")
    parser.add_argument("--loop", action="store_true", help="Keep reviewing pending outcomes on a timer.")
    parser.add_argument("--interval-seconds", type=int, default=300, help="Loop interval. Minimum is 60 seconds.")
    args = parser.parse_args()
    if args.loop:
        return run_loop(
            args.limit,
            include_near_misses=args.include_near_misses,
            dry_run=args.dry_run,
            force=args.force,
            interval_seconds=args.interval_seconds,
        )
    print_review_result(review_alerts(args.limit, include_near_misses=args.include_near_misses, dry_run=args.dry_run, force=args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
