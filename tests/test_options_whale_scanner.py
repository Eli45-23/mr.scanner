import tempfile
import unittest
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scanner.options_whale_scanner import (
    OptionsWhaleScanner,
    apply_index_0dte_noise_filter,
    attach_simple_follow_through,
    build_premium_display_fields,
    build_premium_pressure_fields,
    build_premium_timing_fields,
    build_whale_print_key,
    dedupe_whale_prints,
    format_whale_alert,
)
from scanner.options_whale_storage import OptionsWhaleStorage


class FakeWhaleClient:
    def __init__(self):
        self.contract_calls = []

    def check_access(self):
        return {
            "alpaca_connected": True,
            "options_contracts_available": True,
            "options_snapshots_available": True,
            "options_quotes_available": True,
            "options_trades_available": True,
            "options_bars_available": True,
            "official_options_feed_available": True,
            "last_error": "",
            "data_plan_warning": "",
        }

    def get_assets(self):
        return [{"symbol": "AAPL", "name": "Apple", "status": "active", "tradable": True}]

    def get_option_contracts(self, **kwargs):
        exp = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        requested = kwargs.get("underlying_symbols") or ["AAPL"]
        self.contract_calls.append(list(requested))
        rows = []
        for underlying in requested:
            rows.append({"symbol": f"{underlying}260619C00200000", "underlying_symbol": underlying, "expiration_date": exp, "strike_price": 200, "type": "call", "open_interest": 100})
        return rows

    def get_option_snapshots(self, symbols):
        return {
            symbol: {
                "latestQuote": {"bp": 1.9, "ap": 2.0, "t": datetime.now(timezone.utc).isoformat()},
                "latestTrade": {"p": 2.0, "t": datetime.now(timezone.utc).isoformat()},
                "dailyBar": {"v": 1000, "c": 2.0},
                "trade_count": 3,
                "greeks": {"delta": 0.5},
            }
            for symbol in symbols
        }

    def get_stock_bars(self, symbols, **kwargs):
        return {symbol: [{"o": 198, "h": 201, "l": 197, "c": 200}, {"o": 200, "h": 202, "l": 199, "c": 201}] for symbol in symbols}

    def get_option_trades(self, symbols, **kwargs):
        now = datetime.now(timezone.utc)
        return {symbols[0]: [
            {"timestamp": now.isoformat(), "size": 200, "price": 2.0, "aggression_side": "near_ask"},
            {"timestamp": (now + timedelta(seconds=2)).isoformat(), "size": 200, "price": 2.0, "aggression_side": "near_ask"},
            {"timestamp": (now + timedelta(seconds=4)).isoformat(), "size": 200, "price": 2.0, "aggression_side": "near_ask"},
        ]}


class OptionsWhaleScannerTests(unittest.TestCase):
    def write_universe(self, root: Path, entries):
        path = root / "data" / "options_universe.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": "ok", "entries": entries, "entry_count": len(entries)}), encoding="utf-8")

    def test_full_market_scan_requires_no_watchlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_universe(Path(tmp), [{"underlying_symbol": "AAPL", "contract_count": 10}])
            scanner = OptionsWhaleScanner({
                "options_whale_scanner": {"enabled": True, "max_contracts_per_scan": 10, "min_score": 60, "min_premium": 100000, "min_volume": 500, "min_volume_oi_ratio": 2.0},
                "market_data": {"stock_feed": "sip"},
                "options": {"feed": "opra"},
            }, FakeWhaleClient(), OptionsWhaleStorage(Path(tmp)), root=Path(tmp))
            result = scanner.scan()
            self.assertGreaterEqual(result["results_count"], 1)
            self.assertIn("Possible whale flow", result["results"][0]["message_preview"])
            self.assertIn("first_20_underlyings_scanned", result)
            candidate = result["results"][0]["candidate"]
            self.assertIn("baseline_sample_size", candidate)
            self.assertIn("unusualness_bucket", candidate)
            self.assertTrue(candidate["low_sample_warning"])
            self.assertEqual(result["results"][0]["next_day_oi_status"], "pending")
            self.assertIsNone(result["results"][0]["learned_quality_score"])

    def test_priority_seed_symbols_scan_before_obscure_names_and_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_universe(root, [
                {"underlying_symbol": "AIVC", "contract_count": 1000},
                {"underlying_symbol": "ZZZZ", "contract_count": 900},
                {"underlying_symbol": "AAPL", "contract_count": 10},
                {"underlying_symbol": "SPY", "contract_count": 5},
            ])
            client = FakeWhaleClient()
            scanner = OptionsWhaleScanner({
                "options_whale_scanner": {
                    "enabled": True,
                    "max_contracts_per_scan": 20,
                    "min_score": 99,
                    "min_premium": 999999999,
                    "priority_seed_symbols": ["SPY", "AAPL"],
                    "priority_batch_size": 2,
                },
            }, client, OptionsWhaleStorage(root), root=root)
            result = scanner.scan()
            self.assertEqual(result["first_20_underlyings_scanned"][:2], ["SPY", "AAPL"])
            self.assertIn("AIVC", result["first_20_underlyings_scanned"])
            self.assertIn("ZZZZ", result["first_20_underlyings_scanned"])
            self.assertGreater(result["underlying_symbols_scanned"], 2)
            self.assertEqual(client.contract_calls[0], ["SPY", "AAPL"])

    def test_no_candidate_scan_returns_near_misses_and_rejection_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_universe(root, [{"underlying_symbol": "AAPL", "contract_count": 10}])
            scanner = OptionsWhaleScanner({
                "options_whale_scanner": {
                    "enabled": True,
                    "max_contracts_per_scan": 10,
                    "min_score": 99,
                    "min_premium": 999999999,
                    "min_volume": 500,
                    "min_volume_oi_ratio": 2.0,
                },
            }, FakeWhaleClient(), OptionsWhaleStorage(root), root=root)
            result = scanner.scan()
            self.assertEqual(result["results_count"], 0)
            self.assertGreater(result["near_miss_count"], 0)
            self.assertIn("premium_below_threshold", result["candidate_filter_rejection_summary"])
            self.assertIn("thresholds_failed", result["near_misses"][0])

    def test_snapshot_nested_fields_do_not_zero_volume_or_premium(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_universe(root, [{"underlying_symbol": "AAPL", "contract_count": 10}])
            scanner = OptionsWhaleScanner({
                "options_whale_scanner": {"enabled": True, "max_contracts_per_scan": 10, "min_score": 99, "min_premium": 999999999},
            }, FakeWhaleClient(), OptionsWhaleStorage(root), root=root)
            result = scanner.scan()
            near = result["near_misses"][0]
            self.assertEqual(near["volume"], 1000)
            self.assertGreater(near["premium"], 0)
            self.assertEqual(near["open_interest"], 100)

    def test_debug_loose_mode_returns_results_and_disables_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_universe(root, [{"underlying_symbol": "AAPL", "contract_count": 10}])
            scanner = OptionsWhaleScanner({
                "options_whale_scanner": {
                    "enabled": True,
                    "debug_loose_mode": True,
                    "max_contracts_per_scan": 10,
                    "min_score": 75,
                    "min_premium": 100000,
                    "min_volume": 500,
                    "min_volume_oi_ratio": 2.0,
                },
            }, FakeWhaleClient(), OptionsWhaleStorage(root), root=root)
            result = scanner.scan()
            self.assertTrue(result["debug_loose_mode"])
            self.assertIn("not alert quality", result["debug_label"])
            self.assertGreaterEqual(result["results_count"], 1)
            self.assertFalse(result["results"][0]["should_notify"])

    def test_alert_message_has_required_disclaimer(self):
        message = format_whale_alert({
            "candidate": {"underlying_symbol": "AAPL", "option_symbol": "AAPLX", "option_type": "CALL", "strike": 200, "expiration": "2026-06-19", "estimated_premium": 100000, "volume_oi_ratio": 3},
            "classification": "HIGH WHALE FLOW",
            "direction_label": "Possible bullish call flow",
            "whale_score": 85,
            "alert_tier": "Tier 2",
            "reason_summary": "Large premium.",
            "price_confirmation_label": "needs price confirmation",
        })
        self.assertIn("Possible whale flow — not a trade signal.", message)
        self.assertNotRegex(message.replace("buy/sell", ""), r"\bbuy\b|\bsell\b|\benter\b")

    def test_index_0dte_noise_filter_downgrades_weak_flow(self):
        result = {
            "whale_score": 82,
            "candidate": {
                "underlying_symbol": "SPY",
                "dte": 0,
                "estimated_premium": 100000,
                "spread_percent": 12,
            },
            "price_confirmation_score": 3,
        }
        noise = apply_index_0dte_noise_filter(result, {
            "index_0dte_min_score": 85,
            "index_0dte_min_premium": 250000,
            "index_0dte_max_spread_percent": 8,
            "index_0dte_min_price_confirmation_score": 6,
        })
        self.assertTrue(noise["index_0dte_noise_flag"])
        self.assertLess(noise["noise_adjusted_score"], 82)
        self.assertIn("0DTE index", noise["noise_filter_reason"])

    def test_index_0dte_noise_filter_allows_clean_strong_flow_and_non_0dte(self):
        clean = apply_index_0dte_noise_filter({
            "whale_score": 92,
            "candidate": {"underlying_symbol": "QQQ", "dte": 0, "estimated_premium": 500000, "spread_percent": 4},
            "price_confirmation_score": 8,
        }, {})
        self.assertFalse(clean["index_0dte_noise_flag"])
        self.assertEqual(clean["noise_adjusted_score"], 92)
        non_0dte = apply_index_0dte_noise_filter({
            "whale_score": 70,
            "candidate": {"underlying_symbol": "SPY", "dte": 2, "estimated_premium": 1000, "spread_percent": 50},
            "price_confirmation_score": 0,
        }, {})
        self.assertFalse(non_0dte["index_0dte_noise_flag"])

    def test_price_paid_uses_last_and_falls_back_to_midpoint(self):
        last = build_premium_display_fields({
            "underlying_symbol": "AAPL",
            "option_type": "CALL",
            "strike": 200,
            "expiration": "2026-06-19",
            "moneyness": "ATM",
            "last": 2.5,
            "midpoint": 2.4,
            "estimated_premium": 250000,
        })
        self.assertEqual(last["contract_price_paid"], 2.5)
        self.assertEqual(last["premium_per_contract"], 250)
        fallback = build_premium_display_fields({"last": None, "midpoint": 1.25})
        self.assertEqual(fallback["contract_price_paid"], 1.25)

    def test_timing_delay_handles_missing_trade_time(self):
        timing = build_premium_timing_fields({
            "trade_time": "2026-06-17T14:00:00Z",
            "quote_time": "2026-06-17T14:00:01Z",
            "time_detected": "2026-06-17T14:00:30Z",
        })
        self.assertEqual(timing["premium_trade_delay_seconds"], 30)
        self.assertFalse(timing["stale_trade_print"])
        self.assertEqual(timing["fresh_flow_label"], "fresh premium print")
        missing = build_premium_timing_fields({"time_detected": "2026-06-17T14:00:30Z"})
        self.assertIsNone(missing["premium_trade_delay_seconds"])
        self.assertIn("unavailable", missing["premium_timing_warning"])
        self.assertEqual(missing["fresh_flow_label"], "timing unavailable")
        missing_detected = build_premium_timing_fields({"trade_time": "2026-06-17T14:00:00Z"})
        self.assertFalse(missing_detected["stale_trade_print"])
        self.assertEqual(missing_detected["fresh_flow_label"], "timing unavailable")
        self.assertIn("Scanner detection time unavailable", missing_detected["trade_print_age_warning"])

    def test_stale_trade_print_marks_old_premium_timing(self):
        timing = build_premium_timing_fields({
            "trade_time": "2026-06-17T14:00:00Z",
            "time_detected": "2026-06-17T14:03:00Z",
        })
        self.assertTrue(timing["stale_trade_print"])
        self.assertEqual(timing["trade_print_age_seconds"], 180)
        self.assertEqual(timing["trade_print_age_minutes"], 3)
        self.assertEqual(timing["fresh_flow_label"], "old trade print")
        self.assertIn("more than 2 minutes", timing["trade_print_age_warning"])

    def test_very_stale_trade_print_gets_stronger_label(self):
        timing = build_premium_timing_fields({
            "trade_time": "2026-06-16T14:00:00Z",
            "time_detected": "2026-06-17T14:03:00Z",
        })
        self.assertTrue(timing["stale_trade_print"])
        self.assertEqual(timing["fresh_flow_label"], "stale / old premium print")
        self.assertIn("more than 15 minutes", timing["trade_print_age_warning"])

    def test_whale_print_key_and_dedupe_keep_best_duplicate(self):
        base = {
            "candidate": {
                "option_symbol": "AAPL260619C00200000",
                "trade_time": "2026-06-17T14:00:00Z",
                "last": 2.5,
                "volume": 1000,
                "estimated_premium": 250000,
                "open_interest": 100,
                "time_detected": "2026-06-17T14:00:10Z",
            },
            "whale_score": 80,
        }
        better = {
            "candidate": {**base["candidate"], "time_detected": "2026-06-17T14:00:20Z"},
            "whale_score": 90,
            "reason_summary": "more complete",
        }
        rows = dedupe_whale_prints([base, better])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["whale_score"], 90)
        self.assertEqual(build_whale_print_key(base), build_whale_print_key(better))

    def test_dedupe_keeps_later_trade_time_and_changed_premium(self):
        base = {
            "candidate": {
                "option_symbol": "AAPL260619C00200000",
                "trade_time": "2026-06-17T14:00:00Z",
                "last": 2.5,
                "volume": 1000,
                "estimated_premium": 250000,
                "open_interest": 100,
            },
            "whale_score": 85,
        }
        later = {"candidate": {**base["candidate"], "trade_time": "2026-06-17T14:01:00Z"}, "whale_score": 85}
        changed = {"candidate": {**base["candidate"], "estimated_premium": 300000, "volume": 1200}, "whale_score": 85}
        self.assertEqual(len(dedupe_whale_prints([base, later, changed])), 3)

    def test_pressure_aliases_map_from_existing_aggression(self):
        self.assertEqual(build_premium_pressure_fields({"aggression_side": "near_ask"})["premium_pressure_label"], "ask-side pressure")
        self.assertEqual(build_premium_pressure_fields({"aggression_side": "near_bid"})["premium_pressure_label"], "bid-side pressure")
        self.assertEqual(build_premium_pressure_fields({"aggression_side": "midpoint"})["premium_pressure_label"], "midpoint / unclear")
        self.assertEqual(build_premium_pressure_fields({"aggression_side": "unknown"})["premium_pressure_label"], "unknown")

    def test_simple_follow_through_marks_same_contract_later(self):
        rows = [
            {"candidate": {"option_symbol": "AAPLX", "trade_time": "2026-06-17T14:00:00Z", "last": 1.0, "volume": 100, "estimated_premium": 100000, "time_detected": "2026-06-17T14:00:00Z"}},
            {"candidate": {"option_symbol": "AAPLX", "trade_time": "2026-06-17T14:02:00Z", "last": 1.5, "volume": 100, "estimated_premium": 150000, "time_detected": "2026-06-17T14:02:00Z"}},
        ]
        result = attach_simple_follow_through(rows)
        self.assertEqual(result[0]["follow_through_status"], "no_follow_up_yet")
        self.assertEqual(result[1]["follow_through_status"], "more_premium_added")
        self.assertEqual(result[1]["follow_up_premium"], 150000)

    def test_simple_follow_through_does_not_count_duplicate_print(self):
        rows = [
            {"candidate": {"option_symbol": "AAPLX", "trade_time": "2026-06-17T14:00:00Z", "last": 1.0, "volume": 100, "estimated_premium": 100000, "open_interest": 10, "time_detected": "2026-06-17T14:00:10Z"}},
            {"candidate": {"option_symbol": "AAPLX", "trade_time": "2026-06-17T14:00:00Z", "last": 1.0, "volume": 100, "estimated_premium": 100000, "open_interest": 10, "time_detected": "2026-06-17T14:00:20Z"}},
        ]
        result = attach_simple_follow_through(rows)
        self.assertEqual(result[1]["follow_through_status"], "no_follow_up_yet")
        self.assertIsNone(result[1]["follow_up_premium"])


if __name__ == "__main__":
    unittest.main()
