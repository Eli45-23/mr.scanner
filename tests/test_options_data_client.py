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
    def __init__(self):
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, timeout=0):
        self.calls.append((method, url, params or {}))
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

    def test_refuses_non_market_data_path(self):
        client = OptionsDataClient("key", "secret", session=FakeSession())
        with self.assertRaises(ValueError):
            client._request("GET", "https://paper-api.alpaca.markets", "/v2/orders")

    def test_missing_credentials_do_not_crash(self):
        client = OptionsDataClient("", "", session=FakeSession())
        self.assertFalse(client.check_access()["alpaca_connected"])


if __name__ == "__main__":
    unittest.main()
