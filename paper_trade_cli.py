#!/usr/bin/env python3
"""Paper-only Alpaca CLI helper for scanner experiments.

This module intentionally stays separate from the live scanner. It shells out to
the Alpaca CLI for account checks and paper order previews/submissions, with
guardrails that default to dry-run and block obvious live-trading configuration.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


WATCHLIST = {"AAPL", "QQQ", "META", "SPY", "ASTS", "NVDA"}
LOG_DIR = Path("logs")
PAPER_TRADE_LOG = LOG_DIR / "paper_trade_cli.jsonl"


@dataclass
class CommandResult:
    name: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def is_live_trade_mode() -> bool:
    return os.getenv("ALPACA_LIVE_TRADE", "").strip().lower() == "true"


def cli_path() -> Optional[str]:
    configured = os.getenv("ALPACA_CLI_PATH", "").strip()
    if configured:
        return configured
    return shutil.which("alpaca")


def cli_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["ALPACA_QUIET"] = "1"
    # Let the CLI profile control paper/live/auth instead of scanner API env vars.
    for key in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "APCA_API_BASE_URL",
    ):
        env.pop(key, None)
    return env


def profile_args(profile: Optional[str]) -> List[str]:
    selected = (profile or os.getenv("ALPACA_CLI_PROFILE", "")).strip()
    return ["--profile", selected] if selected else []


def run_cli(name: str, args: List[str], profile: Optional[str], timeout: int = 12) -> CommandResult:
    path = cli_path()
    if not path:
        return CommandResult(name, 127, "", "alpaca CLI not found")
    cmd = [path, "--quiet", *profile_args(profile), *args]
    try:
        completed = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=cli_env(),
            check=False,
        )
        return CommandResult(name, completed.returncode, completed.stdout.strip(), completed.stderr.strip())
    except subprocess.TimeoutExpired:
        return CommandResult(name, 124, "", f"{name} timed out")
    except OSError as exc:
        return CommandResult(name, 126, "", str(exc))


def print_result(result: CommandResult) -> None:
    status = "OK" if result.ok else "ERROR"
    print(f"{result.name}: {status}")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)


def require_safe_mode() -> None:
    if is_live_trade_mode():
        raise SystemExit("Blocked: ALPACA_LIVE_TRADE=true. This helper is paper-only.")


def validate_order_args(args: argparse.Namespace) -> None:
    symbol = args.symbol.upper()
    if symbol not in WATCHLIST and not args.allow_non_watchlist:
        raise SystemExit(f"Blocked: {symbol} is not in the scanner watchlist. Use --allow-non-watchlist to override for paper only.")
    if bool(args.qty) == bool(args.notional):
        raise SystemExit("Provide exactly one of --qty or --notional.")
    if args.side not in {"buy", "sell"}:
        raise SystemExit("--side must be buy or sell.")
    if args.type not in {"market", "limit"}:
        raise SystemExit("This helper currently allows only market or limit paper orders.")
    if args.type == "limit" and not args.limit_price:
        raise SystemExit("--limit-price is required for limit orders.")
    if args.type == "market" and args.limit_price:
        raise SystemExit("--limit-price can only be used with limit orders.")
    if args.execute_paper and args.confirm != "PAPER":
        raise SystemExit("To submit a paper order, pass --execute-paper --confirm PAPER.")


def order_command(args: argparse.Namespace, dry_run: bool) -> List[str]:
    cmd = [
        "order",
        "submit",
        "--symbol",
        args.symbol.upper(),
        "--side",
        args.side,
        "--type",
        args.type,
        "--time-in-force",
        args.time_in_force,
        "--client-order-id",
        f"codex-paper-{datetime.now().strftime('%Y%m%d%H%M%S')}",
    ]
    if args.qty:
        cmd.extend(["--qty", str(args.qty)])
    if args.notional:
        cmd.extend(["--notional", str(args.notional)])
    if args.limit_price:
        cmd.extend(["--limit-price", str(args.limit_price)])
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def log_order_attempt(args: argparse.Namespace, result: CommandResult, dry_run: bool) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "dry_run": dry_run,
        "profile": args.profile or os.getenv("ALPACA_CLI_PROFILE", ""),
        "symbol": args.symbol.upper(),
        "side": args.side,
        "type": args.type,
        "qty": args.qty,
        "notional": args.notional,
        "limit_price": args.limit_price,
        "returncode": result.returncode,
        "ok": result.ok,
    }
    import json

    with PAPER_TRADE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def cmd_health(args: argparse.Namespace) -> int:
    require_safe_mode()
    for result in (
        run_cli("doctor", ["doctor"], args.profile, timeout=10),
        run_cli("clock", ["clock", "--jq", "."], args.profile, timeout=8),
        run_cli("account", ["account", "get", "--jq", "{status: .status, buying_power: .buying_power, portfolio_value: .portfolio_value, trading_blocked: .trading_blocked}"], args.profile, timeout=8),
    ):
        print_result(result)
        print()
        if not result.ok:
            return result.returncode
    return 0


def cmd_preview_order(args: argparse.Namespace) -> int:
    require_safe_mode()
    validate_order_args(args)
    result = run_cli("preview-order", order_command(args, dry_run=True), args.profile, timeout=10)
    print_result(result)
    log_order_attempt(args, result, dry_run=True)
    return result.returncode


def cmd_submit_order(args: argparse.Namespace) -> int:
    require_safe_mode()
    validate_order_args(args)
    result = run_cli("submit-paper-order", order_command(args, dry_run=not args.execute_paper), args.profile, timeout=12)
    print_result(result)
    log_order_attempt(args, result, dry_run=not args.execute_paper)
    return result.returncode


def cmd_list_orders(args: argparse.Namespace) -> int:
    require_safe_mode()
    result = run_cli("list-orders", ["order", "list", "--jq", "."], args.profile, timeout=10)
    print_result(result)
    return result.returncode


def cmd_list_positions(args: argparse.Namespace) -> int:
    require_safe_mode()
    result = run_cli("list-positions", ["position", "list", "--jq", "."], args.profile, timeout=10)
    print_result(result)
    return result.returncode


def cmd_close_position(args: argparse.Namespace) -> int:
    require_safe_mode()
    symbol = args.symbol.upper()
    if symbol not in WATCHLIST and not args.allow_non_watchlist:
        raise SystemExit(f"Blocked: {symbol} is not in the scanner watchlist. Use --allow-non-watchlist to override for paper only.")
    if args.execute_paper and args.confirm != "PAPER":
        raise SystemExit("To close a paper position, pass --execute-paper --confirm PAPER.")
    cmd = ["position", "close", "--symbol-or-asset-id", symbol]
    if args.qty:
        cmd.extend(["--qty", str(args.qty)])
    if args.percentage:
        cmd.extend(["--percentage", str(args.percentage)])
    if not args.execute_paper:
        print("close-position: DRY RUN")
        print(" ".join(["alpaca", *cmd]))
        return 0
    result = run_cli("close-paper-position", cmd, args.profile, timeout=12)
    print_result(result)
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper-only Alpaca CLI helper. Scanner logic is not affected.")
    parser.add_argument("--profile", help="Alpaca CLI profile to use. Defaults to ALPACA_CLI_PROFILE or active CLI profile.")
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="Run paper-safe CLI health checks.")
    health.set_defaults(func=cmd_health)

    def add_order_flags(order_parser: argparse.ArgumentParser) -> None:
        order_parser.add_argument("--symbol", required=True)
        order_parser.add_argument("--side", required=True, choices=["buy", "sell"])
        order_parser.add_argument("--type", default="market", choices=["market", "limit"])
        order_parser.add_argument("--qty")
        order_parser.add_argument("--notional")
        order_parser.add_argument("--limit-price")
        order_parser.add_argument("--time-in-force", default="day")
        order_parser.add_argument("--allow-non-watchlist", action="store_true")

    preview = sub.add_parser("preview-order", help="Dry-run a paper order request. Does not submit.")
    add_order_flags(preview)
    preview.set_defaults(func=cmd_preview_order, execute_paper=False, confirm="")

    submit = sub.add_parser("submit-paper-order", help="Submit only when --execute-paper --confirm PAPER are supplied.")
    add_order_flags(submit)
    submit.add_argument("--execute-paper", action="store_true")
    submit.add_argument("--confirm", default="")
    submit.set_defaults(func=cmd_submit_order)

    list_orders = sub.add_parser("list-orders", help="List paper orders through the CLI profile.")
    list_orders.set_defaults(func=cmd_list_orders)

    list_positions = sub.add_parser("list-positions", help="List paper positions through the CLI profile.")
    list_positions.set_defaults(func=cmd_list_positions)

    close_position = sub.add_parser("close-position", help="Close a paper position only when explicitly confirmed.")
    close_position.add_argument("--symbol", required=True)
    close_position.add_argument("--qty")
    close_position.add_argument("--percentage")
    close_position.add_argument("--allow-non-watchlist", action="store_true")
    close_position.add_argument("--execute-paper", action="store_true")
    close_position.add_argument("--confirm", default="")
    close_position.set_defaults(func=cmd_close_position)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
