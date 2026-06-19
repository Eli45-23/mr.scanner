import unittest
import tempfile
import time
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import scanner_dashboard


class OptionsWhaleDashboardTests(unittest.TestCase):
    def setUp(self):
        self.old_latest = scanner_dashboard.OPTIONS_WHALE_LATEST_PATH

    def tearDown(self):
        scanner_dashboard.OPTIONS_WHALE_LATEST_PATH = self.old_latest
        with scanner_dashboard.STATE.lock:
            scanner_dashboard.STATE.options_whale_scan_running = False
            scanner_dashboard.STATE.options_whale_last_scan_started_at = None
            scanner_dashboard.STATE.options_whale_last_scan_finished_at = None
            scanner_dashboard.STATE.options_whale_last_scan_error = ""
            scanner_dashboard.STATE.options_whale_next_scan_monotonic = 0.0
            scanner_dashboard.STATE.options_whale_auto_scan_paused = False
        while scanner_dashboard.STATE.options_whale_scan_lock.locked():
            scanner_dashboard.STATE.options_whale_scan_lock.release()

    def test_routes_exist_and_scan_has_no_symbol_parameter(self):
        text = Path(scanner_dashboard.__file__).read_text(encoding="utf-8")
        self.assertIn("/api/options-whales/status", text)
        self.assertIn("/api/options-whales/scan", text)
        self.assertIn("/api/options-whales/auto-scan/pause", text)
        self.assertIn("/api/options-whales/auto-scan/resume", text)
        self.assertNotIn("symbols=AAPL", text)
        self.assertIn("Options Whale Scanner", text)
        self.assertIn("OPTIONS_WHALE_FLOW", text)
        self.assertIn("Run Whale Scan Now", text)
        self.assertIn("Scan results are stale", text)

    def test_whale_dashboard_does_not_poll_legacy_endpoints(self):
        html = scanner_dashboard.WHALE_INDEX_HTML
        legacy_paths = [
            "/api/symbols",
            "/api/alerts",
            "/api/alert-brain",
            "/api/status",
            "/api/control-center",
        ]
        for path in legacy_paths:
            self.assertNotIn(path, html)
        self.assertIn("/api/options-whales/status", html)
        self.assertIn("/api/options-whales/latest", html)
        self.assertIn("/api/options-whales/universe/status", html)

    def test_whale_dashboard_debug_candidates_are_hidden_by_default(self):
        html = scanner_dashboard.WHALE_INDEX_HTML
        self.assertIn("Debug Candidates — Not Alerts", html)
        self.assertIn("Show Debug Candidates", html)
        self.assertIn("No real whale alerts passed the filters right now.", html)
        self.assertIn("No real whale alerts right now. Debug candidates are hidden.", html)

    def test_whale_dashboard_formats_utc_timestamps_as_market_time(self):
        html = scanner_dashboard.WHALE_INDEX_HTML
        self.assertIn("function formatMarketTime(value)", html)
        self.assertIn("timeZone:'America/New_York'", html)
        self.assertIn("formatMarketTime(candidateField(item, 'time_detected')", html)
        self.assertIn("Reported trade time ET", html)
        self.assertIn("Quote time ET", html)
        self.assertIn("Scanner detected ET", html)
        self.assertIn("Reported trade time raw", html)

    def test_whale_dashboard_renders_freshness_labels_and_stale_warning(self):
        html = scanner_dashboard.WHALE_INDEX_HTML
        self.assertIn("Fresh premium print", html)
        self.assertIn("Old trade print", html)
        self.assertIn("Stale / old premium print", html)
        self.assertIn("Timing unavailable", html)
        self.assertIn("Trade print age", html)
        self.assertIn("Fresh flow label", html)
        self.assertIn("Stale warning", html)
        self.assertIn("Old premium print — do not treat as fresh flow.", html)
        self.assertIn("Old Premium Prints — Not Fresh Alerts", html)
        self.assertIn("Show Old Premium Prints", html)
        self.assertIn("Fresh premium print.", html)
        self.assertIn("Passed filters", html)
        self.assertIn("Stale quote rejects", html)

    def test_whale_dashboard_symbol_search_accepts_any_ticker_in_data(self):
        html = scanner_dashboard.WHALE_INDEX_HTML
        self.assertIn('id="symbolSearch"', html)
        self.assertIn("function normalizedSymbolSearch()", html)
        self.assertIn(".trim().toUpperCase()", html)
        self.assertIn("function matchesSymbolSearch(item, search)", html)
        self.assertIn("underlying === search || optionSymbol.includes(search)", html)
        self.assertIn("No fresh ${esc(search)} whale alerts right now.", html)
        self.assertNotIn("Invalid ticker", html)
        self.assertNotIn("preset watchlist", html.lower())

    def test_whale_dashboard_detail_explains_real_alerts(self):
        html = scanner_dashboard.WHALE_INDEX_HTML
        for label in (
            "Important Flow Info",
            "Price Paid",
            "Premium Time",
            "Pressure",
            "Follow-through",
            "Why unusual",
            "Bid/ask aggression",
            "Opening / closing estimate",
            "Price confirmation",
            "Multi-leg warning",
            "0DTE / index noise warning",
            "Outcome / learning status",
            "Watch only — not a trade signal",
        ):
            self.assertIn(label, html)

    def test_root_serves_whale_dashboard(self):
        text = Path(scanner_dashboard.__file__).read_text(encoding="utf-8")
        self.assertIn("self.send_html(WHALE_INDEX_HTML)", text)

    def write_latest(self, root: Path, timestamp: str):
        scanner_dashboard.OPTIONS_WHALE_LATEST_PATH = root / "data" / "options_whale_latest.json"
        scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.write_text(json.dumps({
            "timestamp": timestamp,
            "contracts_scanned": 12,
            "candidates_found": 3,
            "near_misses": [{"option_symbol": "AAPLX"}],
            "results": [{"candidate": {"underlying_symbol": "AAPL"}}],
        }), encoding="utf-8")

    def test_latest_marks_stale_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            self.write_latest(Path(tmp), old)
            with mock.patch.object(scanner_dashboard, "options_whale_interval", return_value=30):
                latest = scanner_dashboard.options_whales_latest()
            self.assertTrue(latest["stale"])
            self.assertIn("Auto-scan may not be running", latest["stale_warning"])
            self.assertEqual(len(latest["near_misses"]), 1)

    def test_latest_orders_fresh_rows_before_stale_rows_and_counts_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner_dashboard.OPTIONS_WHALE_LATEST_PATH = Path(tmp) / "data" / "options_whale_latest.json"
            scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
            scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.write_text(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "deduped_results_count": 3,
                "duplicate_results_count": 1,
                "results": [
                    {"whale_score": 99, "candidate": {"underlying_symbol": "OLD", "stale_trade_print": True, "trade_print_age_seconds": 901}},
                    {"whale_score": 70, "candidate": {"underlying_symbol": "FRESH", "stale_trade_print": False, "trade_print_age_seconds": 30}},
                    {"whale_score": 65, "candidate": {"underlying_symbol": "OLD2", "trade_print_age_seconds": 121}},
                ],
            }), encoding="utf-8")
            latest = scanner_dashboard.options_whales_latest()
        symbols = [row["candidate"]["underlying_symbol"] for row in latest["results"]]
        self.assertEqual(symbols, ["FRESH", "OLD", "OLD2"])
        self.assertEqual(latest["fresh_count"], 1)
        self.assertEqual(latest["stale_count"], 2)
        self.assertEqual(latest["deduped_count"], 3)
        self.assertEqual(latest["diagnostics"]["duplicate_results_count"], 1)

    def test_freshness_counts_do_not_count_stale_as_fresh_real_alerts(self):
        rows = [
            {"candidate": {"stale_trade_print": False, "trade_print_age_seconds": 45}},
            {"candidate": {"stale_trade_print": True, "trade_print_age_seconds": 180}},
            {"candidate": {"trade_print_age_seconds": 901}},
        ]
        counts = scanner_dashboard.options_whale_freshness_counts(rows)
        self.assertEqual(counts["fresh_count"], 1)
        self.assertEqual(counts["stale_count"], 2)

    def test_status_returns_scan_runtime_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc).isoformat()
            self.write_latest(Path(tmp), now)
            with mock.patch.object(scanner_dashboard, "options_whale_scanner") as fake_scanner:
                fake_scanner.return_value.status.return_value = {"scanner_name": "Options Whale Scanner"}
                status = scanner_dashboard.options_whales_status()
            self.assertIn("auto_scan_enabled", status)
            self.assertIn("scan_interval_seconds", status)
            self.assertIn("last_scan_age_seconds", status)
            self.assertEqual(status["latest_result_count"], 1)
            self.assertEqual(status["latest_near_miss_count"], 1)
            self.assertEqual(status["contracts_scanned"], 12)

    def test_scan_lock_prevents_overlap(self):
        acquired = scanner_dashboard.STATE.options_whale_scan_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            result = scanner_dashboard.run_options_whale_scan_locked("test")
        finally:
            scanner_dashboard.STATE.options_whale_scan_lock.release()
        self.assertTrue(result["scan_already_running"])
        self.assertEqual(result["message"], "scan already running")

    def test_manual_scan_updates_latest_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner_dashboard.OPTIONS_WHALE_LATEST_PATH = Path(tmp) / "data" / "options_whale_latest.json"

            class FakeScanner:
                def scan(self):
                    return {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "contracts_scanned": 5,
                        "candidates_found": 1,
                        "near_misses": [],
                        "results": [],
                    }

            with mock.patch.object(scanner_dashboard, "options_whale_scanner", return_value=FakeScanner()):
                with mock.patch.object(scanner_dashboard, "send_options_whale_notifications", return_value=None):
                    result = scanner_dashboard.run_options_whale_scan_locked("manual")
            self.assertEqual(result["contracts_scanned"], 5)
            self.assertTrue(scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.exists())

    def test_auto_scan_after_close_preserves_latest_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scanner_dashboard.OPTIONS_WHALE_LATEST_PATH = root / "data" / "options_whale_latest.json"
            scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
            original = {
                "timestamp": "2026-06-18T19:55:00Z",
                "scan_session_state": "regular",
                "results": [{"candidate": {"underlying_symbol": "AAPL"}}],
                "near_misses": [],
            }
            scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.write_text(json.dumps(original), encoding="utf-8")
            fake_scanner = mock.Mock()
            with mock.patch.object(scanner_dashboard, "options_market_session_state", return_value="closed"):
                with mock.patch.object(scanner_dashboard, "options_whale_scanner", return_value=fake_scanner):
                    result = scanner_dashboard.run_options_whale_scan_locked("auto")
            self.assertTrue(result["preserved_regular_session_scan"])
            self.assertIn("preserving last regular-session scan", result["message"])
            fake_scanner.scan.assert_not_called()
            stored = json.loads(scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.read_text(encoding="utf-8"))
            self.assertEqual(stored, original)

    def test_manual_after_close_scan_writes_labeled_after_hours_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner_dashboard.OPTIONS_WHALE_LATEST_PATH = Path(tmp) / "data" / "options_whale_latest.json"

            class FakeScanner:
                def scan(self):
                    return {
                        "timestamp": "2026-06-18T20:05:00Z",
                        "scan_session_state": "closed",
                        "scan_session_warning": "Options market is closed; treat this scan as after-hours/stale context.",
                        "contracts_scanned": 5,
                        "contracts_evaluated": 5,
                        "passed_filter_count": 0,
                        "candidates_found": 0,
                        "near_misses": [],
                        "results": [],
                    }

            with mock.patch.object(scanner_dashboard, "options_market_session_state", return_value="closed"):
                with mock.patch.object(scanner_dashboard, "options_whale_scanner", return_value=FakeScanner()):
                    with mock.patch.object(scanner_dashboard, "send_options_whale_notifications", return_value=None) as notify:
                        result = scanner_dashboard.run_options_whale_scan_locked("manual")
            self.assertEqual(result["scan_session_state"], "closed")
            self.assertIn("after-hours", result["scan_session_warning"])
            self.assertTrue(scanner_dashboard.OPTIONS_WHALE_LATEST_PATH.exists())
            notify.assert_not_called()

    def test_auto_scan_start_is_idempotent(self):
        with mock.patch.object(scanner_dashboard, "options_whale_auto_scan_enabled", return_value=True):
            with mock.patch("threading.Thread") as thread_cls:
                fake_thread = mock.Mock()
                fake_thread.is_alive.return_value = True
                thread_cls.return_value = fake_thread
                with scanner_dashboard.STATE.lock:
                    scanner_dashboard.STATE.options_whale_auto_scan_started = False
                    scanner_dashboard.STATE.options_whale_auto_scan_thread = None
                scanner_dashboard.ensure_options_whale_auto_scan()
                scanner_dashboard.ensure_options_whale_auto_scan()
                self.assertEqual(thread_cls.call_count, 1)


if __name__ == "__main__":
    unittest.main()
