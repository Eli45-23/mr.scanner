#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner


CASE_NAMES = (
    "mixed",
    "do_not_chase",
    "watch_only",
    "trade_quality",
    "context",
    "risk_warning",
)
DISCLAIMER = "Heads-up only — confirm manually. Not a buy/sell signal."


def base_alert(**overrides: object) -> scanner.Alert:
    values = {
        "symbol": "AAPL",
        "timestamp": scanner.now_utc(),
        "category": "PREVIEW ALERT",
        "price": 202.40,
        "direction": "BULLISH",
        "primary_setup": "Bullish Pullback Holding",
        "setup_name": "Bullish Pullback Holding",
        "setup_direction": "BULLISH",
        "setup_stage": "CONFIRMED",
        "setup_reason": "AAPL is above VWAP, EMA9 is rising, and a higher low is forming.",
        "setup_watch_text": "Next candle hold or clean break above the recent high.",
        "scenario_top": {
            "scenario_name": "Bullish Pullback Holding",
            "direction": "bullish",
            "stage": "CONFIRMED",
            "score": 84,
            "invalidation_level": 201.85,
            "invalidation_reason": "Loses VWAP/EMA9 or the recent swing low",
        },
        "scenario_stage": "CONFIRMED",
        "scenario_direction": "bullish",
        "scenario_score": 84,
        "confirmation_score": 68,
        "risk_label": "MEDIUM",
        "entry_quality_label": "GOOD_POSITION",
        "extension_label": "NORMAL",
        "market_regime": "TRENDING_UP",
        "spy_alignment": "ALIGNED",
        "qqq_alignment": "ALIGNED",
        "trend_1m": "BULLISH",
        "trend_5m": "BULLISH",
        "trend_15m": "BULLISH",
        "current_structure_bias": "BULLISH",
        "strategy_levels": {"vwap": 201.85, "ema9": 202.00, "recent_swing_low": 201.70},
        "option_quality": "TRADABLE",
        "option_quality_message": "Option tradable",
        "option_tradable": True,
        "watch_allowed": True,
        "sms_allowed": False,
    }
    values.update(overrides)
    return scanner.Alert(**values)


def sample_alerts() -> Dict[str, scanner.Alert]:
    return {
        "mixed": base_alert(
            setup_name="Mixed Signal",
            primary_setup="Mixed Signal",
            scenario_conflict=True,
            mixed_signal_detected=True,
            mixed_signal_reason="AAPL is below VWAP, but bullish and bearish setup signals disagree.",
            direction="BEARISH",
            setup_direction="NEUTRAL",
            current_structure_bias="BEARISH",
            trend_1m="BEARISH",
            trend_5m="BEARISH",
            trend_15m="BEARISH",
            setup_watch_text="Clean pullback/retest or clear rejection.",
            option_quality_message="Option tradable, but setup is not clean",
        ),
        "do_not_chase": base_alert(
            setup_name="Late Move",
            primary_setup="Bearish Trend Continuation",
            direction="BEARISH",
            setup_direction="BEARISH",
            setup_stage="LATE",
            scenario_stage="LATE",
            scenario_direction="bearish",
            entry_quality_label="LATE",
            risk_label="DO_NOT_CHASE",
            extension_label="VERY_EXTENDED",
            setup_reason="AAPL is below VWAP and structure is bearish, but the move is already extended.",
            setup_watch_text="Pullback/retest or clean rejection.",
            current_structure_bias="BEARISH",
            trend_1m="BEARISH",
            trend_5m="BEARISH",
            trend_15m="BEARISH",
        ),
        "watch_only": base_alert(
            setup_stage="FORMING",
            scenario_stage="FORMING",
            confirmation_score=56,
            option_quality="TRADABLE",
            option_quality_message="Option tradable",
            option_tradable=True,
            setup_reason="AAPL is holding EMA9, but the setup still needs confirmation.",
            setup_watch_text="A confirmed higher low and next-candle hold.",
        ),
        "trade_quality": base_alert(
            sms_allowed=True,
            option_tradable=True,
            option_quality="TRADABLE",
            option_quality_message="Option tradable",
        ),
        "context": base_alert(
            category="WATCH KEY LEVEL APPROACHING",
            primary_setup=None,
            setup_name=None,
            setup_stage=None,
            scenario_top=None,
            scenario_stage=None,
            scenario_direction=None,
            scenario_score=None,
            direction="MOMENTUM",
            confirmation_score=None,
            setup_reason=None,
            setup_watch_text="Clean break-and-hold or rejection.",
            current_structure_bias="NEUTRAL",
            market_regime="RANGE_BOUND",
            spy_alignment="NEUTRAL",
            qqq_alignment="NEUTRAL",
            option_quality=None,
            option_quality_message="Stock setup only",
            option_tradable=False,
            strategy_levels={},
        ),
        "risk_warning": base_alert(
            market_regime="RANGE_BOUND",
            option_quality="WIDE_SPREAD",
            option_quality_message="Option wide spread — stock setup only",
            option_tradable=False,
            confirmation_score=58,
            setup_reason="The setup is developing, but range-bound conditions and option spread add risk.",
            setup_watch_text="Cleaner market alignment and stronger confirmation.",
        ),
    }


def render_cases(case_name: str = "all") -> Dict[str, str]:
    alerts = sample_alerts()
    selected: Iterable[str] = CASE_NAMES if case_name == "all" else (case_name,)
    return {
        name: scanner.professional_telegram_message(alerts[name], "PHASE3_HEADS_UP")
        for name in selected
    }


def validate_message(name: str, message: str) -> Tuple[bool, str]:
    expected = {
        "mixed": "AAPL MIXED / NO TRADE",
        "do_not_chase": "AAPL DO NOT CHASE",
        "watch_only": "AAPL WATCH ONLY",
        "trade_quality": "AAPL TRADE QUALITY WATCH",
        "context": "AAPL CONTEXT ONLY",
        "risk_warning": "AAPL RISK WARNING",
    }[name]
    failures = []
    if not message.startswith(expected):
        failures.append(f"expected conclusion {expected!r}")
    if "Invalidation:" not in message:
        failures.append("missing invalidation")
    if "Option:" not in message:
        failures.append("missing option quality")
    if DISCLAIMER not in message:
        failures.append("missing disclaimer")
    actionable_text = message.replace(DISCLAIMER, "")
    if re.search(r"\b(buy|sell|enter)\b", actionable_text, flags=re.IGNORECASE):
        failures.append("contains buy/sell/enter language")
    for secret_name in ("TELEGRAM_BOT_TOKEN", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "OPENAI_API_KEY"):
        secret = os.getenv(secret_name, "").strip()
        if secret and secret in message:
            failures.append(f"exposes {secret_name}")
    return not failures, "; ".join(failures)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview production Telegram scanner alert text safely.")
    parser.add_argument("--case", choices=(*CASE_NAMES, "all"), default="all")
    parser.add_argument("--send-telegram", action="store_true", help="Explicitly send rendered previews to configured Telegram.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scanner.load_dotenv()
    rendered = render_cases(args.case)
    validation_failed = False
    for name, message in rendered.items():
        valid, reason = validate_message(name, message)
        validation_failed = validation_failed or not valid
        print(f"\n{'=' * 18} {name.upper()} {'=' * 18}")
        print(message)
        print(f"\nValidation: {'PASS' if valid else f'FAIL — {reason}'}")

    if validation_failed:
        return 1
    if not args.send_telegram:
        print("\nPreview only. Telegram was not contacted.")
        return 0

    config = scanner.load_config(None)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    timeout = int(config.get("notifications", {}).get("telegram_timeout_seconds", 8))
    sent_all = True
    for name, message in rendered.items():
        sent, error = scanner.send_telegram_message(
            token=token,
            chat_id=chat_id,
            message=message,
            timeout_seconds=timeout,
            alert_type="PREVIEW",
            alert_source="PREVIEW_TOOL",
            symbol="AAPL",
            message_source_path="tools/preview_alert_text.py",
        )
        sent_all = sent_all and sent
        print(f"Telegram preview {name}: {'sent' if sent else f'failed — {error}'}")
    return 0 if sent_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
