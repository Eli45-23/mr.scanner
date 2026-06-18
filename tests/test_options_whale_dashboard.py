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
