#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

import requests

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
OPENAI_FORMATTER_LOG = ROOT / "logs" / "openai_alert_formatter.jsonl"
OPENAI_SCHEMA_FIELDS = (
    "title",
    "bias",
    "why",
    "market",
    "structure",
    "risk",
    "wait_for",
    "invalidation",
    "option",
    "reminder",
    "final_message",
)
FORBIDDEN_PHRASES = (
    "buy",
    "sell",
    "enter now",
    "get in",
    "take this trade",
    "guaranteed",
    "must trade",
    "considering any action",
)
OPENAI_MAX_CHARS = 750
REQUIRED_SECTION_LABELS = (
    "Why:",
    "Market:",
    "Structure:",
    "Risk:",
    "Wait for:",
    "Invalidation:",
    "Option:",
)


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


def extract_rule_facts(name: str, alert: scanner.Alert, rule_message: str) -> Dict[str, str]:
    lines = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in rule_message.splitlines() if ":" in line}
    direction = str(alert.setup_direction or alert.scenario_direction or alert.direction or "").upper()
    setup = str(alert.setup_name or alert.primary_setup or (alert.scenario_top or {}).get("scenario_name") or "").strip()
    return {
        "case": name,
        "title": rule_message.splitlines()[0].strip(),
        "phone_conclusion": str(alert.phone_conclusion or ""),
        "direction": direction,
        "setup": setup,
        "why": lines.get("Why", ""),
        "market": lines.get("Market", ""),
        "structure": lines.get("Structure", ""),
        "risk": lines.get("Risk", ""),
        "wait_for": lines.get("Wait for", ""),
        "invalidation": lines.get("Invalidation", ""),
        "option": lines.get("Option", ""),
        "reminder": DISCLAIMER,
    }


def extract_openai_output_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    texts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                texts.append(content["text"])
    return "\n".join(texts).strip()


def parse_openai_json(data: Dict[str, Any]) -> Dict[str, Any]:
    text = extract_openai_output_text(data)
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("OpenAI formatter returned non-object JSON")
    return parsed


def assemble_openai_message(output: Dict[str, Any], facts: Dict[str, str]) -> str:
    return "\n\n".join(
        [
            facts["title"],
            f"Why:\n{str(output.get('why') or '').strip()}",
            f"Market:\n{str(output.get('market') or facts['market']).strip()}",
            f"Structure:\n{str(output.get('structure') or facts['structure']).strip()}",
            f"Risk:\n{str(output.get('risk') or '').strip()}",
            f"Wait for:\n{str(output.get('wait_for') or '').strip()}",
            f"Invalidation:\n{facts['invalidation']}",
            f"Option:\n{facts['option']}",
            DISCLAIMER,
        ]
    )


def validate_openai_output(name: str, output: Dict[str, Any], facts: Dict[str, str]) -> Tuple[bool, str]:
    failures = []
    for field in OPENAI_SCHEMA_FIELDS:
        if not isinstance(output.get(field), str) or not output[field].strip():
            failures.append(f"missing {field}")
    if failures:
        return False, "; ".join(failures)

    message = output["final_message"].strip()
    sections = message.split("\n\n")
    expected_sections = 1 + len(REQUIRED_SECTION_LABELS) + 1
    if len(sections) != expected_sections:
        failures.append("must use title, labeled sections, and disclaimer separated by blank lines")
    else:
        if sections[0].strip() != facts["title"]:
            failures.append("title must be its own section")
        for section, label in zip(sections[1:-1], REQUIRED_SECTION_LABELS):
            lines = section.splitlines()
            if len(lines) != 2 or lines[0].strip() != label or not lines[1].strip():
                failures.append(f"{label} must have exactly one short content line")
        if sections[-1].strip() != DISCLAIMER:
            failures.append("disclaimer must be its own final section")
    if output["title"].strip() != facts["title"] or not message.startswith(facts["title"]):
        failures.append("changed locked title/conclusion")
    if output["invalidation"].strip() != facts["invalidation"] or f"Invalidation:\n{facts['invalidation']}" not in message:
        failures.append("changed or omitted invalidation")
    if output["option"].strip() != facts["option"] or f"Option:\n{facts['option']}" not in message:
        failures.append("changed or omitted option quality")
    locked_structure_numbers = re.findall(r"\d+(?:\.\d+)?", facts.get("structure", ""))
    output_structure_numbers = re.findall(r"\d+(?:\.\d+)?", message)
    if any(number not in output_structure_numbers for number in locked_structure_numbers):
        failures.append("changed or omitted locked market-structure numeric level")
    if output["reminder"].strip() != DISCLAIMER or DISCLAIMER not in message:
        failures.append("changed or omitted disclaimer")
    direction = facts["direction"]
    if direction in {"BULLISH", "BEARISH"} and direction.lower() not in output["bias"].lower():
        failures.append("changed or omitted direction")
    setup = facts["setup"]
    if setup and name not in {"mixed", "context", "do_not_chase"} and setup.lower() not in message.lower():
        failures.append("changed or omitted setup name")
    if name == "mixed" and "TRADE QUALITY WATCH" in message.upper():
        failures.append("upgraded mixed signal")
    actionable_text = message.replace(DISCLAIMER, "")
    for phrase in FORBIDDEN_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", actionable_text, flags=re.IGNORECASE):
            failures.append(f"forbidden language: {phrase}")
    max_chars = int(facts.get("max_chars") or OPENAI_MAX_CHARS)
    if len(message) > max_chars:
        failures.append(f"message exceeds {max_chars} characters")
    base_valid, base_reason = validate_message(name, message)
    if not base_valid:
        failures.append(base_reason)
    return not failures, "; ".join(dict.fromkeys(failures))


def append_formatter_log(
    *,
    case_name: str,
    attempted: bool,
    success: bool,
    fallback_used: bool,
    error: str,
    model: str,
    latency_ms: int,
    output_char_count: int,
    setup: str = "",
    fallback_reason: str = "",
    validation_passed: bool = False,
) -> None:
    payload = {
        "timestamp": scanner.now_utc().isoformat(),
        "formatter_attempted": attempted,
        "formatter_success": success,
        "fallback_used": fallback_used,
        "error": scanner.redact_notification_error(error),
        "model": model,
        "latency_ms": latency_ms,
        "case": case_name,
        "setup": setup,
        "fallback_reason": scanner.redact_notification_error(fallback_reason),
        "validation_passed": bool(validation_passed),
        "output_char_count": output_char_count,
    }
    try:
        OPENAI_FORMATTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with OPENAI_FORMATTER_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def openai_model_name() -> str:
    return os.getenv("OPENAI_ALERT_FORMATTER_MODEL", "").strip() or os.getenv("OPENAI_MODEL", "").strip() or "gpt-4.1-mini"


def format_with_openai(
    name: str,
    alert: scanner.Alert,
    rule_message: str,
    *,
    api_key: Optional[str] = None,
    request_fn: Callable[..., Any] = requests.post,
    model: Optional[str] = None,
    max_chars: int = OPENAI_MAX_CHARS,
) -> Dict[str, Any]:
    model = model or openai_model_name()
    key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "").strip()
    started = time.monotonic()
    facts = extract_rule_facts(name, alert, rule_message)
    facts["max_chars"] = str(max_chars)
    if not key:
        error = "OPENAI_API_KEY is missing — using rule-based fallback."
        append_formatter_log(
            case_name=name, attempted=False, success=False, fallback_used=True, error=error,
            model=model, latency_ms=0, output_char_count=len(rule_message), setup=facts["setup"],
            fallback_reason=error, validation_passed=False,
        )
        return {"message": rule_message, "success": False, "fallback_used": True, "error": error, "model": model}

    system_prompt = (
        "You are a strict phone-alert copy editor. Rewrite wording inside labeled sections only. "
        "Never change facts, direction, conclusion, setup, risk, option quality, invalidation, or reminder. "
        "Never soften risk. Never give trading advice or use promotional language. No paragraph-style output. "
        "Each labeled section must contain exactly one short plain-English sentence. No repeated facts. "
        "Return JSON only with exactly: title, bias, why, risk, wait_for, invalidation, option, reminder, final_message."
    )
    user_prompt = (
        "Rewrite the alert using the locked facts below. The title, direction, setup, invalidation, option, and reminder "
        f"are immutable. Keep final_message at or below {max_chars} characters. "
        "Do not use buy, sell, enter now, get in, take this trade, guaranteed, must trade, or considering any action. "
        "Preserve MIXED / NO TRADE, DO NOT CHASE, and CONTEXT ONLY exactly when present. "
        "Use exactly this multiline format, including blank lines and labels:\n\n"
        "<title>\n\nWhy:\n<one short sentence>\n\nMarket:\n<one short sentence>\n\n"
        "Structure:\n<one short sentence>\n\nRisk:\n<one short sentence>\n\n"
        "Wait for:\n<one short sentence>\n\nInvalidation:\n<exact locked invalidation>\n\n"
        "Option:\n<exact locked option>\n\n<exact locked reminder>\n\n"
        "Use the locked direction in bias and keep the locked setup name when one exists.\n\nLOCKED FACTS:\n"
        + json.dumps(facts, separators=(",", ":"))
    )
    schema_properties = {field: {"type": "string"} for field in OPENAI_SCHEMA_FIELDS}
    for locked_field in ("title", "invalidation", "option", "reminder"):
        schema_properties[locked_field] = {"type": "string", "const": facts[locked_field]}
    try:
        response = request_fn(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "instructions": system_prompt,
                "input": user_prompt,
                "max_output_tokens": 900,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "alert_phone_format",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": schema_properties,
                            "required": list(OPENAI_SCHEMA_FIELDS),
                            "additionalProperties": False,
                        },
                    }
                },
            },
            timeout=8,
        )
        response.raise_for_status()
        output = parse_openai_json(response.json())
        output["final_message"] = assemble_openai_message(output, facts)
        valid, reason = validate_openai_output(name, output, facts)
        if not valid:
            raise ValueError(f"OpenAI output rejected: {reason}")
        message = output["final_message"].strip()
        latency_ms = int((time.monotonic() - started) * 1000)
        append_formatter_log(
            case_name=name, attempted=True, success=True, fallback_used=False, error="",
            model=model, latency_ms=latency_ms, output_char_count=len(message), setup=facts["setup"],
            fallback_reason="", validation_passed=True,
        )
        return {"message": message, "success": True, "fallback_used": False, "error": "", "model": model}
    except Exception as exc:
        error = scanner.redact_notification_error(exc, [key])
        latency_ms = int((time.monotonic() - started) * 1000)
        append_formatter_log(
            case_name=name, attempted=True, success=False, fallback_used=True, error=error,
            model=model, latency_ms=latency_ms, output_char_count=len(rule_message), setup=facts["setup"],
            fallback_reason=error, validation_passed=False,
        )
        return {"message": rule_message, "success": False, "fallback_used": True, "error": error, "model": model}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview production Telegram scanner alert text safely.")
    parser.add_argument("--case", choices=(*CASE_NAMES, "all"), default="all")
    parser.add_argument("--send-telegram", action="store_true", help="Explicitly send rendered previews to configured Telegram.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--use-openai", action="store_true", help="Print the OpenAI-formatted version, with safe fallback.")
    mode.add_argument("--compare-openai", action="store_true", help="Print rule-based and OpenAI-formatted versions.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scanner.load_dotenv()
    alerts = sample_alerts()
    rendered = render_cases(args.case)
    selected_messages: Dict[str, str] = {}
    validation_failed = False
    for name, rule_message in rendered.items():
        openai_result = None
        if args.use_openai or args.compare_openai:
            openai_result = format_with_openai(name, alerts[name], rule_message)
        message = openai_result["message"] if openai_result else rule_message
        selected_messages[name] = message
        valid, reason = validate_message(name, message)
        validation_failed = validation_failed or not valid
        print(f"\n{'=' * 18} {name.upper()} {'=' * 18}")
        if args.compare_openai:
            print("RULE-BASED FORMAT\n")
            print(rule_message)
            print("\nOPENAI FORMAT\n")
            print(message)
        else:
            print(message)
        if openai_result and openai_result["fallback_used"]:
            print(f"\nOpenAI formatter fallback: {openai_result['error']}")
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
    for name, message in selected_messages.items():
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
