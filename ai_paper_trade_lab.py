#!/usr/bin/env python3
"""AI-assisted Alpaca paper-trading lab.

OpenAI can propose a paper-only test order from recent scanner output, but this
script requires explicit confirmation before submitting through the Alpaca CLI.
It is intentionally separate from the live scanner and does not change alerts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import paper_trade_cli


APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path("state")
PLAN_PATH = STATE_DIR / "ai_paper_plan.json"
ALLOWED_SIDES = {"buy", "sell"}
ALLOWED_TYPES = {"market", "limit"}
REQUIRED_EXECUTION_CONFIDENCE = 100
SYSTEM_PROMPT = (
    "You are a paper-trading test planner for a local scanner. "
    "This is for Alpaca PAPER trading only, not live trading and not financial advice. "
    "Do not claim the user should buy or sell. Pick at most one paper-test action from the provided scanner data and open positions. "
    "Allowed actions are PAPER_ORDER, CLOSE_POSITION, or NO_TRADE. "
    "Prefer clean, recent scanner signals with aligned direction, strong RVOL, and lower chase risk. "
    "Only return PAPER_ORDER or CLOSE_POSITION when confidence is exactly 100; otherwise return NO_TRADE. "
    "If no clean candidate exists, return action NO_TRADE. "
    "Return JSON only."
)


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


def load_today_alerts(limit: int = 80) -> List[Dict[str, Any]]:
    path = APP_DIR / "logs" / "alerts.jsonl"
    if not path.exists():
        return []
    today = datetime.now().astimezone().date()
    alerts: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            alert = json.loads(line)
            ts = datetime.fromisoformat(str(alert.get("timestamp", "")).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.astimezone().date() == today:
            alert["local_time"] = ts.astimezone().strftime("%H:%M:%S")
            alerts.append(alert)
    useful = [
        {
            "local_time": a.get("local_time"),
            "symbol": a.get("symbol"),
            "direction": a.get("direction"),
            "category": a.get("category"),
            "price": a.get("price"),
            "fast_move_pct": a.get("fast_move_pct"),
            "day_move_pct": a.get("day_move_pct"),
            "relative_volume": a.get("relative_volume"),
            "option_quality": a.get("option_quality"),
            "option_spread_pct": a.get("option_spread_pct"),
            "alert_grade": a.get("alert_grade"),
            "sms_allowed": a.get("sms_allowed"),
            "watch_allowed": a.get("watch_allowed"),
            "market_alignment": a.get("market_alignment"),
            "text_alert_reason": a.get("text_alert_reason"),
        }
        for a in alerts[-limit:]
    ]
    return useful


def openai_plan_payload(alerts: List[Dict[str, Any]], notional: Optional[str], qty: Optional[str]) -> Dict[str, Any]:
    return {
        "task": "choose one paper-trading test action: open paper order, close paper position, or NO_TRADE",
        "allowed_symbols": sorted(paper_trade_cli.WATCHLIST),
        "allowed_actions": ["PAPER_ORDER", "CLOSE_POSITION", "NO_TRADE"],
        "allowed_sides": sorted(ALLOWED_SIDES),
        "allowed_order_types": sorted(ALLOWED_TYPES),
        "default_order_type": "market",
        "requested_notional": notional,
        "requested_qty": qty,
        "execution_confidence_requirement": REQUIRED_EXECUTION_CONFIDENCE,
        "required_schema": {
            "action": "PAPER_ORDER | CLOSE_POSITION | NO_TRADE",
            "symbol": "AAPL|QQQ|META|SPY|ASTS|NVDA|null",
            "side": "buy|sell|null",
            "type": "market|limit|null",
            "qty": "string|null",
            "notional": "string|null",
            "limit_price": "string|null",
            "confidence": 0,
            "reason": "short string",
            "risk_note": "short string",
            "confirm_in_webull": True,
        },
        "recent_alerts": alerts,
        "open_positions": load_open_positions(),
    }


def load_open_positions() -> List[Dict[str, Any]]:
    result = paper_trade_cli.run_cli("list-positions", ["position", "list", "--jq", "."], None, timeout=8)
    if not result.ok or not result.stdout:
        return []
    try:
        parsed = json.loads(result.stdout)
    except Exception:
        return []
    if isinstance(parsed, dict):
        parsed = parsed.get("positions") or parsed.get("data") or []
    if not isinstance(parsed, list):
        return []
    positions = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or item.get("asset_symbol") or "").upper()
        if symbol not in paper_trade_cli.WATCHLIST:
            continue
        positions.append(
            {
                "symbol": symbol,
                "qty": item.get("qty"),
                "side": item.get("side"),
                "market_value": item.get("market_value"),
                "unrealized_pl": item.get("unrealized_pl"),
                "unrealized_plpc": item.get("unrealized_plpc"),
                "current_price": item.get("current_price"),
            }
        )
    return positions


def call_openai_for_plan(alerts: List[Dict[str, Any]], notional: Optional[str], qty: Optional[str]) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set.")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    payload = openai_plan_payload(alerts, notional, qty)
    body = json.dumps(
        {
            "model": model,
            "instructions": SYSTEM_PROMPT,
            "input": "Return JSON only for this paper-trading test payload:\n"
            + json.dumps(payload, separators=(",", ":"), default=str),
            "max_output_tokens": 700,
            "text": {"format": {"type": "json_object"}},
        }
    )
    completed = subprocess.run(
        [
            "curl",
            "-sS",
            "--fail-with-body",
            "--max-time",
            "8",
            "https://api.openai.com/v1/responses",
            "-H",
            f"Authorization: Bearer {api_key}",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
        ],
        input=body,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("OpenAI request failed")
    data = json.loads(completed.stdout)
    text = extract_output_text(data)
    return validate_plan(json.loads(text))


def extract_output_text(data: Dict[str, Any]) -> str:
    chunks: List[str] = []
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
    text = "\n".join(chunks).strip()
    if not text:
        raise ValueError("OpenAI returned no output text")
    return text


def validate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    action = str(plan.get("action") or "NO_TRADE").upper()
    if action not in {"PAPER_ORDER", "CLOSE_POSITION", "NO_TRADE"}:
        action = "NO_TRADE"
    if action == "NO_TRADE":
        return {
            "action": "NO_TRADE",
            "symbol": None,
            "side": None,
            "type": None,
            "qty": None,
            "notional": None,
            "limit_price": None,
            "confidence": clamp_confidence(plan.get("confidence")),
            "reason": str(plan.get("reason") or "No clean paper-test candidate."),
            "risk_note": str(plan.get("risk_note") or "No order prepared."),
            "confirm_in_webull": True,
        }

    symbol = str(plan.get("symbol") or "").upper()
    confidence = clamp_confidence(plan.get("confidence"))
    if confidence < REQUIRED_EXECUTION_CONFIDENCE:
        return {
            "action": "NO_TRADE",
            "symbol": None,
            "side": None,
            "type": None,
            "qty": None,
            "notional": None,
            "limit_price": None,
            "confidence": confidence,
            "reason": "AI confidence below required 100% paper-execution gate.",
            "risk_note": str(plan.get("risk_note") or "No paper action prepared."),
            "confirm_in_webull": True,
        }
    if action == "CLOSE_POSITION":
        if symbol not in paper_trade_cli.WATCHLIST:
            raise ValueError("AI returned an invalid close-position plan")
        return {
            "action": "CLOSE_POSITION",
            "symbol": symbol,
            "side": None,
            "type": None,
            "qty": str(plan.get("qty")) if plan.get("qty") is not None else None,
            "notional": None,
            "limit_price": None,
            "confidence": confidence,
            "reason": str(plan.get("reason") or ""),
            "risk_note": str(plan.get("risk_note") or ""),
            "confirm_in_webull": True,
        }

    side = str(plan.get("side") or "").lower()
    order_type = str(plan.get("type") or "market").lower()
    if symbol not in paper_trade_cli.WATCHLIST or side not in ALLOWED_SIDES or order_type not in ALLOWED_TYPES:
        raise ValueError("AI returned an invalid paper order plan")
    qty = plan.get("qty")
    notional = plan.get("notional")
    if bool(qty) == bool(notional):
        raise ValueError("AI plan must include exactly one of qty or notional")
    limit_price = plan.get("limit_price")
    if order_type == "limit" and not limit_price:
        raise ValueError("AI limit order plan requires limit_price")
    if order_type == "market":
        limit_price = None
    return {
        "action": "PAPER_ORDER",
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "qty": str(qty) if qty is not None else None,
        "notional": str(notional) if notional is not None else None,
        "limit_price": str(limit_price) if limit_price is not None else None,
        "confidence": confidence,
        "reason": str(plan.get("reason") or ""),
        "risk_note": str(plan.get("risk_note") or ""),
        "confirm_in_webull": True,
    }


def clamp_confidence(value: Any) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except Exception:
        return 0


def save_plan(plan: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    wrapped = {"created_at": datetime.now().astimezone().isoformat(), "plan": plan}
    PLAN_PATH.write_text(json.dumps(wrapped, indent=2, sort_keys=True), encoding="utf-8")


def load_plan() -> Dict[str, Any]:
    if not PLAN_PATH.exists():
        raise SystemExit("No AI paper plan exists yet. Run plan first.")
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))["plan"]


def print_plan(plan: Dict[str, Any]) -> None:
    print(json.dumps(plan, indent=2, sort_keys=True))
    if plan.get("action") in {"PAPER_ORDER", "CLOSE_POSITION"}:
        print("\nPrepared PAPER test only. Confirm in Webull. To submit:")
        print("python3 ai_paper_trade_lab.py execute --execute-paper --confirm PAPER")


def cmd_plan(args: argparse.Namespace) -> int:
    paper_trade_cli.require_safe_mode()
    alerts = load_today_alerts(limit=args.max_alerts)
    plan = call_openai_for_plan(alerts, args.notional, args.qty)
    if plan["action"] == "PAPER_ORDER":
        if args.qty:
            plan["qty"] = args.qty
            plan["notional"] = None
        if args.notional:
            plan["notional"] = args.notional
            plan["qty"] = None
    save_plan(plan)
    print_plan(plan)
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    paper_trade_cli.require_safe_mode()
    plan = load_plan()
    if plan.get("action") not in {"PAPER_ORDER", "CLOSE_POSITION"}:
        print_plan(plan)
        return 0
    if int(plan.get("confidence") or 0) < REQUIRED_EXECUTION_CONFIDENCE:
        raise SystemExit("Blocked. AI confidence is below the required 100% paper-execution gate.")
    if not args.execute_paper or args.confirm != "PAPER":
        raise SystemExit("Blocked. To submit the stored paper order, pass --execute-paper --confirm PAPER.")
    if plan.get("action") == "CLOSE_POSITION":
        close_args = argparse.Namespace(
            profile=args.profile,
            symbol=plan["symbol"],
            qty=plan.get("qty"),
            percentage=None,
            allow_non_watchlist=False,
            execute_paper=True,
            confirm="PAPER",
        )
        return paper_trade_cli.cmd_close_position(close_args)
    order_args = argparse.Namespace(
        profile=args.profile,
        symbol=plan["symbol"],
        side=plan["side"],
        type=plan["type"],
        qty=plan.get("qty"),
        notional=plan.get("notional"),
        limit_price=plan.get("limit_price"),
        time_in_force="day",
        allow_non_watchlist=False,
        execute_paper=True,
        confirm="PAPER",
    )
    return paper_trade_cli.cmd_submit_order(order_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-assisted PAPER trade lab. Does not affect scanner alerts.")
    parser.add_argument("--profile", help="Alpaca CLI profile.")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Ask OpenAI for one paper-test candidate or NO_TRADE.")
    plan.add_argument("--qty")
    plan.add_argument("--notional", default="100")
    plan.add_argument("--max-alerts", type=int, default=80)
    plan.set_defaults(func=cmd_plan)
    execute = sub.add_parser("execute", help="Submit the stored plan to Alpaca PAPER only.")
    execute.add_argument("--execute-paper", action="store_true")
    execute.add_argument("--confirm", default="")
    execute.set_defaults(func=cmd_execute)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
