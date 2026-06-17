import unittest

from scanner.options_data_client import OptionsDataClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="OK"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {"X-RateLimit-Remaining": "99"}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, overrides=None):
        self.headers = {}
        self.calls = []
        self.overrides = overrides or {}

    def request(self, method, url, params=None, timeout=0):
        self.calls.append((method, url, params or {}))
        for key, response in self.overrides.items():
            if key in url:
                return response
        if "/v2/stocks/bars/latest" in url:
            return FakeResponse(payload={"bars": {"AAPL": {"c": 200}}})
        if "/v2/options/contracts" in url:
            return FakeResponse(payload={"option_contracts": [{"symbol": "AAPL260619C00200000"}]})
        if "/v1beta1/options/snapshots" in url:
            return FakeResponse(payload={"snapshots": {"AAPL260619C00200000": {}}})
        if "/v1beta1/options/quotes/latest" in url:
            return FakeResponse(payload={"quotes": {"AAPL260619C00200000": {"bp": 1, "ap": 1.1}}})
        if "/v1beta1/options/trades" in url or "/v1beta1/options/bars" in url:
            return FakeResponse(payload={})
        return FakeResponse(status_code=404, text="missing")


class OptionsDataClientTests(unittest.TestCase):
    def test_access_check_graceful_success(self):
        client = OptionsDataClient("key", "secret", session=FakeSession())
        status = client.check_access()
        self.assertTrue(status["alpaca_connected"])
        self.assertTrue(status["options_contracts_available"])
        self.assertTrue(status["options_snapshots_available"])
        self.assertIn("contracts_url_used", status)
        self.assertIn("/v2/options/contracts", status["contracts_url_used"])
        self.assertEqual(status["paper_or_live_mode"], "paper")

    def test_refuses_non_market_data_path(self):
        client = OptionsDataClient("key", "secret", session=FakeSession())
        with self.assertRaises(ValueError):
            client._request("GET", "https://paper-api.alpaca.markets", "/v2/orders")

    def test_missing_credentials_do_not_crash(self):
        client = OptionsDataClient("", "", session=FakeSession())
        self.assertFalse(client.check_access()["alpaca_connected"])

    def test_contracts_base_url_selection_is_safe_by_default(self):
        client = OptionsDataClient("key", "secret", session=FakeSession())
        self.assertEqual(client.options_contracts_base_url, "https://paper-api.alpaca.markets")
        self.assertEqual(client.paper_or_live_mode, "paper")

        custom = OptionsDataClient(
            "key",
            "secret",
            session=FakeSession(),
            options_contracts_base_url="https://example.test/",
        )
        self.assertEqual(custom.options_contracts_base_url, "https://example.test")
        self.assertEqual(custom.paper_or_live_mode, "custom")

        live = OptionsDataClient("key", "secret", session=FakeSession(), live_trade=True)
        self.assertEqual(live.options_contracts_base_url, "https://api.alpaca.markets")
        self.assertEqual(live.paper_or_live_mode, "live")

    def test_401_contracts_diagnostic_wording(self):
        session = FakeSession({"/v2/options/contracts": FakeResponse(status_code=401, text='{"code":40110000,"message":"request is not authorized"}')})
        client = OptionsDataClient("key", "secret", session=session)
        status = client.check_access()
        self.assertFalse(status["options_contracts_available"])
        self.assertIn("endpoint is unauthorized", status["entitlement_hint"])
        self.assertIn("contracts", status["endpoint_diagnostics"])
        self.assertEqual(status["endpoint_diagnostics"]["contracts"]["http_status"], 401)

    def test_403_snapshot_diagnostic_wording(self):
        session = FakeSession({"/v1beta1/options/snapshots": FakeResponse(status_code=403, text="forbidden")})
        client = OptionsDataClient("key", "secret", session=session)
        status = client.check_access()
        self.assertFalse(status["options_snapshots_available"])
        self.assertIn("additional entitlement", status["entitlement_hint"])

    def test_debug_tool_source_does_not_print_secrets_or_call_order_paths(self):
        from pathlib import Path

        text = Path("tools/debug_alpaca_options_endpoints.py").read_text(encoding="utf-8")
        self.assertNotIn("ALPACA_SECRET_KEY", text)
        self.assertNotIn("APCA-API-SECRET-KEY", text)
        self.assertNotIn("/v2/orders", text)

    def test_historical_trades_and_bars_do_not_send_feed_param(self):
        from datetime import datetime, timezone

        session = FakeSession()
        client = OptionsDataClient("key", "secret", session=session)
        now = datetime.now(timezone.utc)
        client.get_option_trades(["AAPL260619C00200000"], start=now, end=now)
        client._request(
            "GET",
            client.options_data_base_url,
            "/v1beta1/options/bars",
            params={"symbols": "AAPL260619C00200000", "timeframe": "1Min", "start": now.isoformat(), "limit": 1},
        )
        trades_call = [call for call in session.calls if "/v1beta1/options/trades" in call[1]][-1]
        bars_call = [call for call in session.calls if "/v1beta1/options/bars" in call[1]][-1]
        self.assertNotIn("feed", trades_call[2])
        self.assertNotIn("feed", bars_call[2])


if __name__ == "__main__":
    unittest.main()
