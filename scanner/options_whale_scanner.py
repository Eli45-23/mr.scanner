from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from scanner.options_block_detector import detect_block_print
from scanner.options_data_client import OptionsDataClient
from scanner.options_flow_classifier import classify_aggression, estimate_opening_flow, apply_multileg_direction_adjustment
from scanner.options_multileg_detector import default_multileg_result, detect_possible_multileg
from scanner.options_oi_review import review_alerts_with_next_day_oi
from scanner.options_price_context import classify_price_context
from scanner.options_sweep_detector import approximate_sweep_from_snapshot, detect_sweep_activity
from scanner.options_universe import build_optionable_universe, default_universe_path, load_universe_cache, universe_status
from scanner.options_whale_models import DISCLAIMER, OptionFlowCandidate, utc_now_iso
from scanner.options_whale_scoring import estimated_premium, midpoint, safe_float, score_options_whale_flow, spread_percent, volume_oi_ratio
from scanner.options_whale_storage import OptionsWhaleStorage


DEFAULT_PRIORITY_SEEDS = [
    "SPY", "QQQ", "IWM", "DIA", "NVDA", "AAPL", "TSLA", "AMD", "MSFT", "META",
    "AMZN", "GOOGL", "NFLX", "AVGO", "COIN", "MSTR", "SMH", "XLK", "XLF", "XLE",
    "XLV", "XLI", "XLY", "XLP", "XLU", "TLT", "HYG", "GLD", "SLV",
]

FORBIDDEN_ALERT_PHRASES = (
    "b" + "uy this",
    "s" + "ell this",
    "enter " + "now",
    "enter " + "trade",
    "guaran" + "teed",
    "confirmed smart " + "money",
)
FORBIDDEN_ALERT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(phrase) for phrase in FORBIDDEN_ALERT_PHRASES) + r")\b",
    re.IGNORECASE,
)


def default_options_whale_config() -> Dict[str, Any]:
    return {
        "enabled": True,
        "legacy_momentum_enabled": False,
        "full_market": True,
        "max_dte": 7,
        "include_0dte": True,
        "include_weeklies": True,
        "min_score": 75,
        "min_premium": 100000,
        "min_volume": 500,
        "min_volume_oi_ratio": 2.0,
        "max_spread_percent": 15,
        "scan_interval_seconds": 30,
        "max_contracts_per_scan": 10000,
        "max_results": 100,
        "enable_sweep_detection": True,
        "enable_block_detection": True,
        "enable_multileg_detection": True,
        "enable_price_action_context": True,
        "enable_next_day_oi_review": True,
        "enable_notifications": True,
        "notify_tier_2": False,
        "debug_loose_mode": False,
        "priority_seed_symbols": DEFAULT_PRIORITY_SEEDS,
        "priority_batch_size": 50,
    }


def whale_config(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = default_options_whale_config()
    merged.update(config.get("options_whale_scanner", {}))
    return merged


def _contract_symbol(row: Dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("option_symbol") or row.get("id") or "").upper()


def _contract_underlying(row: Dict[str, Any]) -> str:
    value = row.get("underlying_symbol") or row.get("underlying_asset_symbol") or row.get("root_symbol")
    if value:
        return str(value).upper()
    match = re.match(r"^([A-Z]+)\d{6}[CP]\d+", _contract_symbol(row))
    return match.group(1) if match else ""


def _contract_type(row: Dict[str, Any]) -> str:
    raw = str(row.get("type") or row.get("option_type") or "").upper()
    if raw in {"CALL", "C"}:
        return "CALL"
    if raw in {"PUT", "P"}:
        return "PUT"
    match = re.search(r"\d{6}([CP])\d+", _contract_symbol(row))
    return "CALL" if match and match.group(1) == "C" else "PUT" if match else "UNKNOWN"


def _contract_expiration(row: Dict[str, Any]) -> str:
    raw = row.get("expiration_date") or row.get("expiration")
    if raw:
        return str(raw)[:10]
    match = re.search(r"(\d{6})[CP]\d+", _contract_symbol(row))
    if not match:
        return date.today().isoformat()
    text = match.group(1)
    return f"20{text[:2]}-{text[2:4]}-{text[4:6]}"


def _contract_strike(row: Dict[str, Any]) -> float:
    raw = row.get("strike_price") or row.get("strike")
    if raw is not None:
        return safe_float(raw)
    match = re.search(r"[CP](\d{8})$", _contract_symbol(row))
    return safe_float(match.group(1)) / 1000 if match else 0.0


def _snapshot_quote(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return snapshot.get("latestQuote") or snapshot.get("latest_quote") or snapshot.get("q") or {}


def _snapshot_trade(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return snapshot.get("latestTrade") or snapshot.get("latest_trade") or snapshot.get("t") or {}


def _snapshot_bar(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return snapshot.get("dailyBar") or snapshot.get("daily_bar") or snapshot.get("day") or {}


def _timestamp(raw: Dict[str, Any]) -> Optional[str]:
    value = raw.get("t") or raw.get("timestamp") or raw.get("time")
    return str(value) if value else None


def _quote_age_seconds(timestamp: Optional[str], now: datetime) -> Optional[float]:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds())
    except ValueError:
        return None


def _moneyness(option_type: str, strike: float, underlying_price: Optional[float]) -> tuple[str, Optional[float], Optional[float]]:
    if not underlying_price or underlying_price <= 0 or strike <= 0:
        return "UNKNOWN", None, None
    distance = strike - underlying_price
    pct = distance / underlying_price * 100
    if abs(pct) <= 1.0:
        label = "ATM"
    elif (option_type == "CALL" and strike < underlying_price) or (option_type == "PUT" and strike > underlying_price):
        label = "ITM"
    else:
        label = "OTM"
    return label, round(distance, 4), round(pct, 2)


def result_alert_tier(result: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[str, bool, str]:
    score = int(result.get("whale_score") or 0)
    candidate = result.get("candidate") or {}
    spread = candidate.get("spread_percent")
    warnings = candidate.get("warnings") or []
    if score >= 90 and result.get("aggression_side") == "near_ask" and safe_float(candidate.get("estimated_premium")) >= float(cfg.get("min_premium", 100000)) and (spread is None or safe_float(spread) <= cfg.get("max_spread_percent", 15)) and result.get("price_context_score", 0) >= 6:
        return "Tier 1", True, "Extreme score, aggressive flow, acceptable spread, and price context."
    if score >= 80 and not any("wide spread" in str(w).lower() or "stale" in str(w).lower() for w in warnings):
        return "Tier 2", bool(cfg.get("notify_tier_2", False)), "High score with minor or no quality warnings."
    if score >= 75:
        return "Tier 3", False, "Unusual but unclear; watch only."
    return "Ignore", False, "Below whale-flow threshold."


def format_whale_alert(result: Dict[str, Any]) -> str:
    candidate = result.get("candidate") or {}
    lines = [
        f"{candidate.get('underlying_symbol', 'UNKNOWN')} {result.get('classification', 'POSSIBLE WHALE FLOW')}",
        f"{result.get('direction_label', 'Mixed / unclear flow')} | Score {result.get('whale_score', 0)} | {result.get('alert_tier', 'Tier 3')}",
        f"Contract: {candidate.get('option_symbol')} {candidate.get('option_type')} {candidate.get('strike')} exp {candidate.get('expiration')}",
        f"Premium: ${safe_float(candidate.get('estimated_premium')):,.0f} | Vol/OI: {candidate.get('volume_oi_ratio')}",
        f"Reason: {result.get('reason_summary', 'Unusual options activity detected.')}",
        f"Price context: {result.get('price_confirmation_label', 'Needs price confirmation')}",
        DISCLAIMER,
        "Watch only. Needs price confirmation.",
    ]
    message = "\n".join(str(line) for line in lines if line)
    if FORBIDDEN_ALERT_RE.search(message.replace(DISCLAIMER, "")):
        raise ValueError("Forbidden alert wording generated")
    return message


class OptionsWhaleScanner:
    def __init__(self, config: Dict[str, Any], client: OptionsDataClient, storage: OptionsWhaleStorage, *, root: Optional[Path] = None) -> None:
        self.config = config
        self.whale = whale_config(config)
        self.client = client
        self.storage = storage
        self.root = root or Path.cwd()
        self.universe_path = self.root / "data" / "options_universe.json"
        self.last_scan: Dict[str, Any] = {}
        self.latest_results: List[Dict[str, Any]] = []
        self.last_scan_order: Dict[str, Any] = {}

    def status(self) -> Dict[str, Any]:
        access = self.client.check_access()
        return {
            "scanner_name": "Options Whale Scanner",
            "enabled": bool(self.whale.get("enabled", True)),
            "legacy_momentum_enabled": bool(self.whale.get("legacy_momentum_enabled", False)),
            **access,
            "last_scan_time": self.last_scan.get("timestamp"),
            "universe": universe_status(self.universe_path),
            "latest_result_count": len(self.latest_results),
            "data_plan_warning": access.get("data_plan_warning") or access.get("last_error") or "",
        }

    def rebuild_universe(self) -> Dict[str, Any]:
        return build_optionable_universe(self.client, self.config, cache_path=self.universe_path)

    def universe_status(self) -> Dict[str, Any]:
        return universe_status(self.universe_path)

    def _priority_seed_symbols(self) -> List[str]:
        raw = self.whale.get("priority_seed_symbols") or DEFAULT_PRIORITY_SEEDS
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",")]
        out: List[str] = []
        seen = set()
        for item in raw:
            symbol = str(item or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                out.append(symbol)
        return out

    def _prioritized_underlyings(self, entries: List[Dict[str, Any]]) -> List[str]:
        by_symbol = {
            str(entry.get("underlying_symbol") or "").upper(): entry
            for entry in entries
            if entry.get("underlying_symbol")
        }
        seeds = self._priority_seed_symbols()
        ordered: List[str] = []
        seen = set()
        for symbol in seeds:
            if symbol not in seen:
                ordered.append(symbol)
                seen.add(symbol)
        rest = sorted(
            (entry for symbol, entry in by_symbol.items() if symbol not in seen),
            key=lambda item: (-int(item.get("contract_count") or 0), str(item.get("underlying_symbol") or "")),
        )
        for entry in rest:
            symbol = str(entry.get("underlying_symbol") or "").upper()
            if symbol and symbol not in seen:
                ordered.append(symbol)
                seen.add(symbol)
        return ordered

    def _contracts(self) -> List[Dict[str, Any]]:
        cache = load_universe_cache(self.universe_path)
        today = datetime.now(timezone.utc).date()
        max_dte = int(self.whale.get("max_dte", 7))
        max_contracts = int(self.whale.get("max_contracts_per_scan", 10000))
        entries = [entry for entry in cache.get("entries", []) if entry.get("underlying_symbol")]
        if not entries:
            universe = self.rebuild_universe()
            entries = [entry for entry in universe.get("entries", []) if entry.get("underlying_symbol")]
        underlyings = self._prioritized_underlyings(entries)
        self.last_scan_order = {
            "universe_size": len(entries),
            "underlying_symbols_considered": len(underlyings),
            "underlying_symbols_scanned": 0,
            "first_20_underlyings_scanned": [],
            "last_20_underlyings_scanned": [],
            "contracts_scanned_by_underlying": {},
        }
        if not self.whale.get("full_market", True):
            underlyings = underlyings[:100]
        contracts: List[Dict[str, Any]] = []
        seen_contracts = set()
        scanned_underlyings: List[str] = []
        batch_size = max(1, int(self.whale.get("priority_batch_size", 50)))
        for idx in range(0, len(underlyings), batch_size):
            if len(contracts) >= max_contracts:
                break
            batch = underlyings[idx: idx + batch_size]
            remaining = max_contracts - len(contracts)
            rows = self.client.get_option_contracts(
                expiration_gte=today,
                expiration_lte=today + timedelta(days=max_dte),
                underlying_symbols=batch,
                limit=min(10000, remaining),
                max_contracts=remaining,
            )
            scanned_underlyings.extend(batch)
            for row in rows:
                symbol = _contract_symbol(row)
                if not symbol or symbol in seen_contracts:
                    continue
                seen_contracts.add(symbol)
                contracts.append(row)
                underlying = _contract_underlying(row)
                counts = self.last_scan_order["contracts_scanned_by_underlying"]
                counts[underlying] = counts.get(underlying, 0) + 1
                if len(contracts) >= max_contracts:
                    break
        self.last_scan_order.update({
            "underlying_symbols_scanned": len(scanned_underlyings),
            "first_20_underlyings_scanned": scanned_underlyings[:20],
            "last_20_underlyings_scanned": scanned_underlyings[-20:],
        })
        return contracts

    def _underlying_prices(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        end = datetime.now(timezone.utc)
        bars = self.client.get_stock_bars(symbols, start=end - timedelta(minutes=20), end=end)
        prices: Dict[str, Optional[float]] = {}
        for symbol, rows in bars.items():
            prices[symbol] = safe_float((rows[-1] if rows else {}).get("c") or (rows[-1] if rows else {}).get("close")) if rows else None
        return prices

    def _candidate_from_contract(self, contract: Dict[str, Any], snapshot: Dict[str, Any], prices: Dict[str, Optional[float]], now: datetime) -> OptionFlowCandidate:
        symbol = _contract_symbol(contract)
        underlying = _contract_underlying(contract)
        option_type = _contract_type(contract)
        expiration = _contract_expiration(contract)
        strike = _contract_strike(contract)
        quote = _snapshot_quote(snapshot)
        trade = _snapshot_trade(snapshot)
        bar = _snapshot_bar(snapshot)
        greeks = snapshot.get("greeks") or {}
        bid = safe_float(quote.get("bp") or quote.get("bid_price") or quote.get("bid"))
        ask = safe_float(quote.get("ap") or quote.get("ask_price") or quote.get("ask"))
        last = safe_float(trade.get("p") or trade.get("price") or snapshot.get("latestPrice") or bar.get("c") or contract.get("close_price"))
        mid = midpoint(bid, ask)
        spread = round(ask - bid, 4) if bid and ask else None
        spread_pct = spread_percent(bid, ask)
        volume = int(safe_float(snapshot.get("volume") or snapshot.get("day_volume") or snapshot.get("dailyVolume") or bar.get("v")))
        oi = snapshot.get("open_interest") or snapshot.get("openInterest") or contract.get("open_interest")
        voi = volume_oi_ratio(volume, oi)
        quote_time = _timestamp(quote)
        trade_time = _timestamp(trade)
        underlying_price = prices.get(underlying)
        money, distance, distance_pct = _moneyness(option_type, strike, underlying_price)
        exp_date = date.fromisoformat(expiration)
        dte = max(0, (exp_date - now.date()).days)
        premium = estimated_premium(volume, last, bid, ask)
        warnings: List[str] = []
        if not bid or not ask:
            warnings.append("zero bid/ask or missing quote")
        if spread_pct is not None and spread_pct > float(self.whale.get("max_spread_percent", 15)):
            warnings.append("wide spread")
        age = _quote_age_seconds(quote_time, now)
        if age is None:
            warnings.append("missing quote timestamp")
        elif age > 120:
            warnings.append("stale quote")
        if dte == 0:
            warnings.append("0DTE high-risk contract")
        if money == "OTM" and distance_pct is not None and abs(distance_pct) > 8 and premium < float(self.whale.get("min_premium", 100000)) * 3:
            warnings.append("far OTM warning")
        return OptionFlowCandidate(
            time_detected=utc_now_iso(),
            underlying_symbol=underlying,
            underlying_price=underlying_price,
            option_symbol=symbol,
            contract_id=contract.get("id"),
            option_type=option_type,
            expiration=expiration,
            dte=dte,
            strike=strike,
            moneyness=money,
            distance_from_underlying_price=distance,
            distance_percent=distance_pct,
            bid=bid or None,
            ask=ask or None,
            last=last or None,
            midpoint=mid,
            spread=spread,
            spread_percent=spread_pct,
            volume=volume,
            open_interest=int(safe_float(oi)) if oi is not None else None,
            volume_oi_ratio=voi,
            implied_volatility=safe_float(snapshot.get("impliedVolatility") or snapshot.get("implied_volatility") or snapshot.get("iv")) or None,
            delta=safe_float(greeks.get("delta")) if greeks else None,
            gamma=safe_float(greeks.get("gamma")) if greeks else None,
            theta=safe_float(greeks.get("theta")) if greeks else None,
            vega=safe_float(greeks.get("vega")) if greeks else None,
            trade_count=int(safe_float(snapshot.get("trade_count") or snapshot.get("tradeCount"))) if snapshot.get("trade_count") or snapshot.get("tradeCount") else None,
            quote_time=quote_time,
            trade_time=trade_time,
            quote_freshness_seconds=age,
            estimated_premium=premium,
            data_source=snapshot.get("data_source") or "alpaca",
            warnings=warnings,
        )

    def _effective_whale_config(self) -> Dict[str, Any]:
        cfg = dict(self.whale)
        if cfg.get("debug_loose_mode", False):
            cfg.update({
                "min_score": min(int(cfg.get("min_score", 75)), 40),
                "min_premium": min(float(cfg.get("min_premium", 100000)), 1000.0),
                "min_volume": min(int(cfg.get("min_volume", 500)), 1),
                "min_volume_oi_ratio": 0.0,
                "max_spread_percent": max(float(cfg.get("max_spread_percent", 15)), 100.0),
                "enable_notifications": False,
            })
        return cfg

    def _filter_rejections(self, candidate: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> List[str]:
        cfg = cfg or self.whale
        reasons: List[str] = []
        if candidate.get("dte", 999) > int(cfg.get("max_dte", 7)):
            reasons.append("dte_above_max")
        if candidate.get("dte") == 0 and not cfg.get("include_0dte", True):
            reasons.append("0dte_disabled")
        if safe_float(candidate.get("estimated_premium")) < float(cfg.get("min_premium", 100000)):
            reasons.append("premium_below_threshold")
        if int(candidate.get("volume") or 0) < int(cfg.get("min_volume", 500)):
            reasons.append("volume_below_threshold")
        if candidate.get("volume_oi_ratio") is not None and safe_float(candidate.get("volume_oi_ratio")) < float(cfg.get("min_volume_oi_ratio", 2.0)):
            reasons.append("volume_oi_below_threshold")
        if candidate.get("spread_percent") is not None and safe_float(candidate.get("spread_percent")) > float(cfg.get("max_spread_percent", 15)):
            reasons.append("spread_above_threshold")
        warnings = [str(w).lower() for w in candidate.get("warnings", [])]
        if any("zero bid/ask" in warning for warning in warnings):
            reasons.append("zero_bid_ask")
        if any("stale quote" in warning for warning in warnings):
            reasons.append("stale_quote")
        return reasons

    def _passes_filters(self, candidate: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> bool:
        return not self._filter_rejections(candidate, cfg)

    def _snapshot_field_diagnostic(self, contract: Dict[str, Any], snapshot: Dict[str, Any], candidate: Dict[str, Any], score: int) -> Dict[str, Any]:
        return {
            "underlying": candidate.get("underlying_symbol"),
            "option_symbol": candidate.get("option_symbol"),
            "raw_snapshot_keys": sorted(snapshot.keys()),
            "parsed_bid": candidate.get("bid"),
            "parsed_ask": candidate.get("ask"),
            "parsed_last": candidate.get("last"),
            "parsed_volume": candidate.get("volume"),
            "parsed_open_interest": candidate.get("open_interest"),
            "parsed_trade_time": candidate.get("trade_time"),
            "parsed_quote_time": candidate.get("quote_time"),
            "calculated_premium": candidate.get("estimated_premium"),
            "calculated_spread_percent": candidate.get("spread_percent"),
            "calculated_score": score,
        }

    def scan(self) -> Dict[str, Any]:
        start = datetime.now(timezone.utc)
        if not self.whale.get("enabled", True):
            return {"enabled": False, "results": [], "message": "Options Whale Scanner disabled."}
        effective_cfg = self._effective_whale_config()
        debug_loose = bool(self.whale.get("debug_loose_mode", False))
        contracts = self._contracts()
        option_symbols = [_contract_symbol(c) for c in contracts if _contract_symbol(c)]
        max_contracts = int(self.whale.get("max_contracts_per_scan", 10000))
        snapshots = self.client.get_option_snapshots(option_symbols[:max_contracts])
        underlyings = sorted({_contract_underlying(c) for c in contracts if _contract_underlying(c)})
        prices = self._underlying_prices(underlyings[:500])
        end = datetime.now(timezone.utc)
        stock_bars = self.client.get_stock_bars(underlyings[:50], start=end - timedelta(minutes=90), end=end) if self.whale.get("enable_price_action_context", True) else {}
        raw_candidates: List[Dict[str, Any]] = []
        evaluated: List[Dict[str, Any]] = []
        skipped_reasons: Dict[str, int] = {}
        rejection_summary: Dict[str, int] = {}
        snapshot_field_diagnostics: List[Dict[str, Any]] = []
        for contract in contracts:
            symbol = _contract_symbol(contract)
            if symbol not in snapshots:
                skipped_reasons["missing_snapshot"] = skipped_reasons.get("missing_snapshot", 0) + 1
                continue
            candidate = self._candidate_from_contract(contract, snapshots[symbol], prices, end).to_dict()
            context = classify_price_context(candidate["underlying_symbol"], candidate["option_type"], candidate.get("underlying_price"), stock_bars.get(candidate["underlying_symbol"], [])) if self.whale.get("enable_price_action_context", True) else {}
            candidate.update(classify_aggression(candidate))
            candidate.update(estimate_opening_flow(candidate))
            candidate.update(context)
            approximate = approximate_sweep_from_snapshot(candidate)
            block = detect_block_print(candidate, [], {"min_premium": effective_cfg.get("min_premium", 100000)}) if self.whale.get("enable_block_detection", True) else {}
            scored = score_options_whale_flow({**candidate, **approximate, **block}, context, effective_cfg)
            reasons = self._filter_rejections(candidate, effective_cfg)
            if scored["whale_score"] < int(effective_cfg.get("min_score", 75)):
                reasons.append("score_below_threshold")
            for reason in reasons:
                rejection_summary[reason] = rejection_summary.get(reason, 0) + 1
            record = {
                "candidate": candidate,
                **scored,
                "filter_rejection_reasons": reasons,
                "reason_rejected": ", ".join(reasons) if reasons else "",
            }
            evaluated.append(record)
            if candidate["underlying_symbol"] in {"SPY", "QQQ", "NVDA", "AAPL"} and len(snapshot_field_diagnostics) < 10:
                snapshot_field_diagnostics.append(self._snapshot_field_diagnostic(contract, snapshots[symbol], candidate, scored["whale_score"]))
            if not reasons:
                raw_candidates.append(candidate)
        trade_map: Dict[str, List[Dict[str, Any]]] = {}
        if self.whale.get("enable_sweep_detection", True) and raw_candidates:
            try:
                trade_map = self.client.get_option_trades([c["option_symbol"] for c in raw_candidates[:300]], start=end - timedelta(minutes=3), end=end)
            except Exception:
                trade_map = {}
        multileg_map = detect_possible_multileg(raw_candidates) if self.whale.get("enable_multileg_detection", True) else {}
        results: List[Dict[str, Any]] = []
        for candidate in raw_candidates:
            trades = trade_map.get(candidate["option_symbol"], [])
            aggression = classify_aggression(candidate)
            candidate.update(aggression)
            sweep = detect_sweep_activity(trades) if trades else approximate_sweep_from_snapshot(candidate)
            block = detect_block_print(candidate, trades, {"min_premium": self.whale.get("min_premium", 100000)}) if self.whale.get("enable_block_detection", True) else {}
            multileg = multileg_map.get(candidate["option_symbol"], default_multileg_result())
            flow = apply_multileg_direction_adjustment(aggression, multileg)
            opening = estimate_opening_flow(candidate)
            context = classify_price_context(candidate["underlying_symbol"], candidate["option_type"], candidate.get("underlying_price"), stock_bars.get(candidate["underlying_symbol"], [])) if self.whale.get("enable_price_action_context", True) else {}
            candidate.update(sweep)
            candidate.update(block)
            candidate.update(opening)
            candidate.update(context)
            scored = score_options_whale_flow({**candidate, **sweep, **block, **flow}, context, effective_cfg)
            if scored["whale_score"] < int(effective_cfg.get("min_score", 75)):
                continue
            result = {
                "candidate": candidate,
                **scored,
                **flow,
                **sweep,
                **block,
                **multileg,
                **opening,
                **context,
            }
            tier, should_notify, notify_reason = result_alert_tier(result, effective_cfg)
            if debug_loose:
                should_notify = False
                notify_reason = "DEBUG LOOSE MODE — not alert quality; notifications disabled."
            result.update({"alert_tier": tier, "should_notify": should_notify, "notify_reason": notify_reason, "disclaimer": DISCLAIMER})
            if debug_loose:
                result["debug_loose_mode"] = True
                result["debug_label"] = "DEBUG LOOSE MODE — not alert quality"
            result["message_preview"] = format_whale_alert(result)
            results.append(result)
        results.sort(key=lambda item: int(item.get("whale_score") or 0), reverse=True)
        results = results[: int(effective_cfg.get("max_results", 100))]
        near_misses = sorted(
            evaluated,
            key=lambda item: (int(item.get("whale_score") or 0), safe_float((item.get("candidate") or {}).get("estimated_premium"))),
            reverse=True,
        )[:20]
        near_misses_out = []
        for item in near_misses:
            candidate = item.get("candidate") or {}
            near_misses_out.append({
                "option_symbol": candidate.get("option_symbol"),
                "underlying": candidate.get("underlying_symbol"),
                "volume": candidate.get("volume"),
                "open_interest": candidate.get("open_interest"),
                "premium": candidate.get("estimated_premium"),
                "spread_percent": candidate.get("spread_percent"),
                "score": item.get("whale_score"),
                "reason_rejected": item.get("reason_rejected"),
                "thresholds_failed": item.get("filter_rejection_reasons", []),
            })
        scan_record = {
            "timestamp": utc_now_iso(),
            "duration_seconds": round((datetime.now(timezone.utc) - start).total_seconds(), 2),
            "contracts_scanned": len(option_symbols),
            "candidates_found": len(raw_candidates),
            "results_count": len(results),
            "partial_scan": len(option_symbols) >= max_contracts,
            "partial_scan_warning": "Rate limited or contract cap reached — showing partial scan results." if len(option_symbols) >= max_contracts else "",
            "debug_loose_mode": debug_loose,
            "debug_label": "DEBUG LOOSE MODE — not alert quality" if debug_loose else "",
            **self.last_scan_order,
            "skipped_contracts_count": sum(skipped_reasons.values()),
            "skipped_reasons_summary": skipped_reasons,
            "candidate_filter_rejection_summary": rejection_summary,
            "top_rejection_reasons": sorted(rejection_summary.items(), key=lambda item: item[1], reverse=True)[:10],
            "near_misses": near_misses_out,
            "near_miss_count": len(near_misses_out),
            "snapshot_field_diagnostics": snapshot_field_diagnostics,
            "results": results,
        }
        self.last_scan = scan_record
        self.latest_results = results
        self.storage.append_scan({k: v for k, v in scan_record.items() if k != "results"})
        for result in results:
            if result.get("should_notify"):
                self.storage.append_alert(result)
        return scan_record

    def history(self, limit: int = 100) -> Dict[str, Any]:
        return {"alerts": self.storage.latest_alerts(limit=limit)}

    def latest(self) -> Dict[str, Any]:
        return {"results": self.latest_results, "last_scan": {k: v for k, v in self.last_scan.items() if k != "results"}}

    def review_next_day_oi(self, oi_by_contract: Dict[str, int]) -> List[Dict[str, Any]]:
        reviews = review_alerts_with_next_day_oi(self.storage.latest_alerts(limit=10000), oi_by_contract)
        for record in reviews:
            self.storage.append_oi_review(record)
        return reviews
