#!/usr/bin/env python3
"""SPY-only Alpaca paper autotrader.

This is intentionally separate from the scanner. It reads scanner alerts and can
open or close SPY paper positions through the Alpaca CLI when strict rules pass.
It never trades live and never changes scanner alerts.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import paper_trade_cli


APP_DIR = Path(__file__).resolve().parent
STATE_DIR = APP_DIR / "state"
LOG_DIR = APP_DIR / "logs"
ALERTS_PATH = LOG_DIR / "alerts.jsonl"
STATE_PATH = STATE_DIR / "spy_paper_autotrader_state.json"
SYMBOL = "SPY"
REQUIRED_CONFIDENCE = 100
OPTION_ROOT = "SPY"


def load_dotenv(path: Path = APP_DIR / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def load_recent_spy_alerts(limit: int = 200) -> List[Dict[str, Any]]:
    if not ALERTS_PATH.exists():
        return []
    alerts: List[Dict[str, Any]] = []
    for line in ALERTS_PATH.read_text(encoding="utf-8").splitlines()[-limit * 3 :]:
        if not line.strip():
            continue
        try:
            alert = json.loads(line)
        except Exception:
            continue
        if str(alert.get("symbol") or "").upper() == SYMBOL:
            alert["_dt"] = parse_dt(alert.get("timestamp"))
            alerts.append(alert)
    alerts = [a for a in alerts if a.get("_dt")]
    alerts.sort(key=lambda item: item["_dt"])
    return alerts[-limit:]


def text_has_bad_reason(alert: Dict[str, Any]) -> bool:
    reason = str(alert.get("text_alert_reason") or "").lower()
    notes = " | ".join(str(note) for note in alert.get("notes") or []).lower()
    bad_terms = (
        "stale",
        "extended",
        "opposes",
        "cooldown",
        "repeat",
        "not fresh",
        "below text-alert threshold",
        "has not held",
        "mixed",
        "unknown",
    )
    return any(term in reason or term in notes for term in bad_terms)


def direction_agrees(alert: Dict[str, Any]) -> bool:
    direction = str(alert.get("direction") or "").upper()
    fast = float(alert.get("fast_move_pct") or 0.0)
    day = float(alert.get("day_move_pct") or 0.0)
    if direction == "BULLISH":
        return fast >= 0.0 and day >= 0.15
    if direction == "BEARISH":
        return fast <= 0.0 and day <= -0.15
    return False


def eligible_spy_alert(alert: Dict[str, Any], max_age_seconds: int) -> tuple[bool, List[str]]:
    reasons: List[str] = []
    now = datetime.now().astimezone()
    dt = alert.get("_dt")
    if not dt or (now - dt).total_seconds() > max_age_seconds:
        reasons.append("alert is not recent enough")
    if alert.get("symbol") != SYMBOL:
        reasons.append("not SPY")
    if not alert.get("sms_allowed"):
        reasons.append("not a full scanner alert")
    if alert.get("watch_allowed"):
        reasons.append("watch alerts are not eligible")
    if str(alert.get("alert_grade") or "") != "A+":
        reasons.append("grade is not A+")
    if int(alert.get("alert_score") or 0) < 90:
        reasons.append("score below 90")
    if str(alert.get("market_alignment") or "") != "ALIGNED":
        reasons.append("market alignment is not ALIGNED")
    if str(alert.get("option_quality") or "") != "Tradable":
        reasons.append("option quality is not Tradable")
    if not alert.get("option_contract"):
        reasons.append("missing selected SPY option contract")
    if float(alert.get("option_spread_pct") or 999.0) > 5.0:
        reasons.append("option spread above 5%")
    if float(alert.get("relative_volume") or 0.0) < 2.0:
        reasons.append("RVOL below 2.0x")
    if text_has_bad_reason(alert):
        reasons.append("alert reason/notes include a blocker")
    if not direction_agrees(alert):
        reasons.append("fast/day move do not agree with direction")
    return not reasons, reasons


def expected_option_type(direction: str) -> str:
    return "CALL" if direction == "BULLISH" else "PUT"


def option_contract_matches_direction(alert: Dict[str, Any]) -> bool:
    direction = str(alert.get("direction") or "").upper()
    option_type = str(alert.get("option_type") or "").upper()
    return option_type == expected_option_type(direction)


def is_spy_option_symbol(symbol: Any) -> bool:
    raw = str(symbol or "").upper()
    return raw.startswith(OPTION_ROOT) and len(raw) > len(OPTION_ROOT) + 8 and ("C" in raw[len(OPTION_ROOT):] or "P" in raw[len(OPTION_ROOT):])


def option_direction_from_symbol(symbol: Any) -> Optional[str]:
    raw = str(symbol or "").upper()
    tail = raw[len(OPTION_ROOT):] if raw.startswith(OPTION_ROOT) else raw
    if "C" in tail:
        return "BULLISH"
    if "P" in tail:
        return "BEARISH"
    return None


def confidence_for_alert(alert: Dict[str, Any], max_age_seconds: int) -> tuple[int, List[str]]:
    ok, reasons = eligible_spy_alert(alert, max_age_seconds)
    if ok and not option_contract_matches_direction(alert):
        ok = False
        reasons.append("selected option type does not match alert direction")
    return (REQUIRED_CONFIDENCE if ok else 0), reasons


def latest_eligible_spy_alert(max_age_seconds: int) -> tuple[Optional[Dict[str, Any]], int, List[str]]:
    alerts = load_recent_spy_alerts()
    if not alerts:
        return None, 0, ["no SPY alerts found"]
    latest_reasons: List[str] = []
    for alert in reversed(alerts):
        confidence, reasons = confidence_for_alert(alert, max_age_seconds)
        if confidence == REQUIRED_CONFIDENCE:
            return alert, confidence, []
        if not latest_reasons:
            latest_reasons = reasons
    return alerts[-1], 0, latest_reasons


def load_spy_position(profile: Optional[str]) -> Optional[Dict[str, Any]]:
    result = paper_trade_cli.run_cli("list-positions", ["position", "list", "--jq", "."], profile, timeout=8)
    if not result.ok or not result.stdout:
        return None
    try:
        parsed = json.loads(result.stdout)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    for position in parsed:
        if str(position.get("symbol") or position.get("asset_symbol") or "").upper() == SYMBOL:
            return position
    return None


def load_spy_option_position(profile: Optional[str]) -> Optional[Dict[str, Any]]:
    result = paper_trade_cli.run_cli("list-positions", ["position", "list", "--jq", "."], profile, timeout=8)
    if not result.ok or not result.stdout:
        return None
    try:
        parsed = json.loads(result.stdout)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    for position in parsed:
        symbol = str(position.get("symbol") or position.get("asset_symbol") or "").upper()
        if is_spy_option_symbol(symbol):
            return position
    return None


def position_direction(position: Optional[Dict[str, Any]]) -> Optional[str]:
    if not position:
        return None
    side = str(position.get("side") or "").lower()
    if side == "long":
        return "BULLISH"
    if side == "short":
        return "BEARISH"
    try:
        qty = float(position.get("qty") or 0)
    except Exception:
        qty = 0.0
    if qty > 0:
        return "BULLISH"
    if qty < 0:
        return "BEARISH"
    return None


def option_position_direction(position: Optional[Dict[str, Any]]) -> Optional[str]:
    if not position:
        return None
    symbol = position.get("symbol") or position.get("asset_symbol")
    return option_direction_from_symbol(symbol)


def build_decision(args: argparse.Namespace) -> Dict[str, Any]:
    paper_trade_cli.require_safe_mode()
    alert, confidence, reasons = latest_eligible_spy_alert(args.max_alert_age_seconds)
    position = load_spy_option_position(args.profile) if args.instrument == "options" else load_spy_position(args.profile)
    current_direction = option_position_direction(position) if args.instrument == "options" else position_direction(position)
    alert_direction = str((alert or {}).get("direction") or "").upper()
    state = load_state()
    last_used_alert = state.get("last_used_alert_timestamp")
    alert_timestamp = (alert or {}).get("timestamp")

    if position and confidence == REQUIRED_CONFIDENCE and alert_direction and alert_direction != current_direction:
        return {
            "action": "CLOSE_POSITION",
            "symbol": SYMBOL,
            "confidence": REQUIRED_CONFIDENCE,
            "reason": f"Eligible opposite SPY signal {alert_direction} against current paper position {current_direction}.",
            "alert_timestamp": alert_timestamp,
            "position_direction": current_direction,
        }

    if position:
        return {
            "action": "NO_TRADE",
            "symbol": SYMBOL,
            "confidence": 0,
            "reason": f"SPY paper {args.instrument} position already open ({current_direction}); waiting for eligible opposite close signal.",
            "blockers": reasons,
        }

    if confidence != REQUIRED_CONFIDENCE or not alert:
        return {
            "action": "NO_TRADE",
            "symbol": SYMBOL,
            "confidence": 0,
            "reason": "No SPY setup met the 100-confidence paper-trade gate.",
            "blockers": reasons,
        }

    if alert_timestamp and alert_timestamp == last_used_alert:
        return {
            "action": "NO_TRADE",
            "symbol": SYMBOL,
            "confidence": 0,
            "reason": "Latest eligible SPY alert was already used.",
        }

    if args.instrument == "stock" and alert_direction == "BEARISH" and not args.allow_paper_shorts:
        return {
            "action": "NO_TRADE",
            "symbol": SYMBOL,
            "confidence": 0,
            "reason": "Eligible bearish SPY signal found, but paper shorts are disabled.",
            "alert_timestamp": alert_timestamp,
        }

    side = "buy" if alert_direction == "BULLISH" else "sell"
    if args.instrument == "options":
        option_contract = str(alert.get("option_contract") or "").upper()
        return {
            "action": "PAPER_OPTION_ORDER",
            "symbol": option_contract,
            "underlying": SYMBOL,
            "side": "buy",
            "type": "market",
            "qty": str(args.option_qty),
            "confidence": REQUIRED_CONFIDENCE,
            "reason": f"Eligible SPY {alert_direction} scanner alert passed all 100-confidence rules; buying selected {alert.get('option_type')} contract.",
            "alert_timestamp": alert_timestamp,
            "source_alert": {
                "timestamp": alert.get("timestamp"),
                "category": alert.get("category"),
                "direction": alert.get("direction"),
                "price": alert.get("price"),
                "rvol": alert.get("relative_volume"),
                "grade": alert.get("alert_grade"),
                "option_contract": alert.get("option_contract"),
                "option_type": alert.get("option_type"),
                "option_spread_pct": alert.get("option_spread_pct"),
            },
        }
    return {
        "action": "PAPER_ORDER",
        "symbol": SYMBOL,
        "side": side,
        "type": "market",
        "notional": str(args.notional),
        "confidence": REQUIRED_CONFIDENCE,
        "reason": f"Eligible SPY {alert_direction} scanner alert passed all 100-confidence rules.",
        "alert_timestamp": alert_timestamp,
        "source_alert": {
            "timestamp": alert.get("timestamp"),
            "category": alert.get("category"),
            "direction": alert.get("direction"),
            "price": alert.get("price"),
            "rvol": alert.get("relative_volume"),
            "grade": alert.get("alert_grade"),
        },
    }


def execute_decision(decision: Dict[str, Any], args: argparse.Namespace) -> int:
    if decision["action"] == "NO_TRADE":
        print(json.dumps(decision, indent=2, sort_keys=True))
        return 0
    if not args.execute_paper or args.confirm != "PAPER":
        print(json.dumps(decision, indent=2, sort_keys=True))
        raise SystemExit("Dry run only. To let it trade paper by itself, pass --execute-paper --confirm PAPER.")

    if decision["action"] == "CLOSE_POSITION":
        close_symbol = SYMBOL
        if args.instrument == "options":
            position = load_spy_option_position(args.profile)
            close_symbol = str((position or {}).get("symbol") or (position or {}).get("asset_symbol") or "")
            if not is_spy_option_symbol(close_symbol):
                raise SystemExit("Blocked. No SPY option paper position found to close.")
            result = paper_trade_cli.run_cli(
                "close-paper-option-position",
                ["position", "close", "--symbol-or-asset-id", close_symbol],
                args.profile,
                timeout=12,
            )
            paper_trade_cli.print_result(result)
            code = result.returncode
        else:
            close_args = argparse.Namespace(
                profile=args.profile,
                symbol=SYMBOL,
                qty=None,
                percentage=None,
                allow_non_watchlist=False,
                execute_paper=True,
                confirm="PAPER",
            )
            code = paper_trade_cli.cmd_close_position(close_args)
    elif decision["action"] == "PAPER_OPTION_ORDER":
        result = paper_trade_cli.run_cli(
            "submit-paper-spy-option-order",
            [
                "order",
                "submit",
                "--symbol",
                decision["symbol"],
                "--side",
                "buy",
                "--type",
                "market",
                "--qty",
                decision["qty"],
                "--time-in-force",
                "day",
                "--client-order-id",
                f"codex-spy-option-paper-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            ],
            args.profile,
            timeout=12,
        )
        paper_trade_cli.print_result(result)
        code = result.returncode
    else:
        order_args = argparse.Namespace(
            profile=args.profile,
            symbol=SYMBOL,
            side=decision["side"],
            type="market",
            qty=None,
            notional=decision["notional"],
            limit_price=None,
            time_in_force="day",
            allow_non_watchlist=False,
            execute_paper=True,
            confirm="PAPER",
        )
        code = paper_trade_cli.cmd_submit_order(order_args)

    if code == 0:
        state = load_state()
        state["last_action"] = decision["action"]
        state["last_action_at"] = datetime.now().astimezone().isoformat()
        state["last_used_alert_timestamp"] = decision.get("alert_timestamp")
        save_state(state)
    return code


def cmd_once(args: argparse.Namespace) -> int:
    decision = build_decision(args)
    return execute_decision(decision, args)


def cmd_loop(args: argparse.Namespace) -> int:
    print("Starting SPY paper autotrader loop. Press Ctrl-C to stop.")
    while True:
        try:
            decision = build_decision(args)
            execute_decision(decision, args)
            time.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            print("Stopped.")
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SPY-only paper autotrader using scanner alerts.")
    parser.add_argument("--profile", help="Alpaca CLI profile.")
    parser.add_argument("--instrument", choices=["options", "stock"], default="options")
    parser.add_argument("--option-qty", default="1")
    parser.add_argument("--notional", default="100")
    parser.add_argument("--max-alert-age-seconds", type=int, default=120)
    parser.add_argument("--allow-paper-shorts", action="store_true")
    parser.add_argument("--execute-paper", action="store_true")
    parser.add_argument("--confirm", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("once", help="Evaluate and optionally execute one SPY paper action.")
    once.set_defaults(func=cmd_once)
    loop = sub.add_parser("loop", help="Continuously evaluate SPY paper actions.")
    loop.add_argument("--interval-seconds", type=int, default=10)
    loop.set_defaults(func=cmd_loop)
    return parser


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
