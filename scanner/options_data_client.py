from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

import requests


DATA_BASE = "https://data.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
READ_ONLY_PATHS = (
    "/v2/account",
    "/v2/options/contracts",
    "/v1beta1/options/snapshots",
    "/v1beta1/options/quotes",
    "/v1beta1/options/trades",
    "/v1beta1/options/bars",
    "/v2/stocks",
    "/v2/assets",
)


@dataclass
class OptionsAccessStatus:
    alpaca_connected: bool = False
    options_contracts_available: bool = False
    options_snapshots_available: bool = False
    options_quotes_available: bool = False
    options_trades_available: bool = False
    options_bars_available: bool = False
    official_options_feed_available: bool = False
    last_error: str = ""
    data_plan_warning: str = ""
    rate_limit_status: Dict[str, Any] = field(default_factory=dict)
    contracts_url_used: str = ""
    data_url_used: str = ""
    paper_or_live_mode: str = "paper"
    endpoint_diagnostics: Dict[str, Any] = field(default_factory=dict)
    entitlement_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alpaca_connected": self.alpaca_connected,
            "options_contracts_available": self.options_contracts_available,
            "options_snapshots_available": self.options_snapshots_available,
            "options_quotes_available": self.options_quotes_available,
            "options_trades_available": self.options_trades_available,
            "options_bars_available": self.options_bars_available,
            "official_options_feed_available": self.official_options_feed_available,
            "last_error": self.last_error,
            "data_plan_warning": self.data_plan_warning,
            "rate_limit_status": self.rate_limit_status,
            "contracts_url_used": self.contracts_url_used,
            "data_url_used": self.data_url_used,
            "paper_or_live_mode": self.paper_or_live_mode,
            "endpoint_diagnostics": self.endpoint_diagnostics,
            "entitlement_hint": self.entitlement_hint,
        }


def _iso(dt: date | datetime) -> str:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return dt.isoformat()


def chunked(values: List[str], size: int) -> Iterable[List[str]]:
    for idx in range(0, len(values), size):
        yield values[idx: idx + size]


def _clean_base_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _env_base(name: str, default: str) -> str:
    return _clean_base_url(os.getenv(name, default) or default)


def _short_body(response: requests.Response, limit: int = 500) -> str:
    return (response.text or "")[:limit]


def endpoint_hint(status_code: int, endpoint: str = "endpoint") -> str:
    if status_code == 401:
        return (
            "Authenticated to Alpaca, but this endpoint is unauthorized. "
            "Check whether the endpoint base URL is correct and whether this API key has options contract/data permissions."
        )
    if status_code == 403:
        return "Options market data may require additional entitlement/subscription."
    if status_code >= 400:
        return f"{endpoint} returned HTTP {status_code}."
    return ""


class OptionsDataClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        *,
        stock_feed: str = "sip",
        options_feed: str = "opra",
        allow_indicative_fallback: bool = True,
        session: Optional[requests.Session] = None,
        paper_base_url: Optional[str] = None,
        live_base_url: Optional[str] = None,
        trading_base_url: Optional[str] = None,
        options_contracts_base_url: Optional[str] = None,
        options_data_base_url: Optional[str] = None,
        live_trade: Optional[bool] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self.stock_feed = (stock_feed or "sip").lower()
        self.options_feed = (options_feed or "opra").lower()
        self.allow_indicative_fallback = allow_indicative_fallback
        self.session = session or requests.Session()
        self.paper_base_url = _clean_base_url(paper_base_url or _env_base("ALPACA_PAPER_BASE_URL", PAPER_BASE))
        self.live_base_url = _clean_base_url(live_base_url or _env_base("ALPACA_LIVE_BASE_URL", LIVE_BASE))
        self.trading_base_url = _clean_base_url(trading_base_url or os.getenv("ALPACA_TRADING_BASE_URL", ""))
        self.options_data_base_url = _clean_base_url(options_data_base_url or _env_base("ALPACA_OPTIONS_DATA_BASE_URL", DATA_BASE))
        env_live_trade = str(os.getenv("ALPACA_LIVE_TRADE", "")).strip().lower() in {"1", "true", "yes", "on"}
        self.live_trade = bool(env_live_trade if live_trade is None else live_trade)
        explicit_contracts_base = _clean_base_url(options_contracts_base_url or os.getenv("ALPACA_OPTIONS_CONTRACTS_BASE_URL", ""))
        if explicit_contracts_base:
            self.options_contracts_base_url = explicit_contracts_base
            self.paper_or_live_mode = "custom"
        elif self.trading_base_url:
            self.options_contracts_base_url = self.trading_base_url
            self.paper_or_live_mode = "custom_trading_base"
        elif self.live_trade:
            self.options_contracts_base_url = self.live_base_url
            self.paper_or_live_mode = "live"
        else:
            self.options_contracts_base_url = self.paper_base_url
            self.paper_or_live_mode = "paper"
        if self.api_key and self.secret_key:
            self.session.headers.update({
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            })

    def _request(self, method: str, base: str, path: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> requests.Response:
        if not any(path.startswith(prefix) for prefix in READ_ONLY_PATHS):
            raise ValueError(f"Refusing non-market-data path: {path}")
        response = self.session.request(method, f"{base}{path}", params=params or {}, timeout=timeout)
        return response

    def endpoint_url(self, base: str, path: str) -> str:
        return urljoin(f"{base}/", path.lstrip("/"))

    def _diagnostic(self, name: str, response: requests.Response, base: str, path: str) -> Dict[str, Any]:
        return {
            "endpoint": name,
            "base_url": base,
            "url": self.endpoint_url(base, path),
            "path": path,
            "http_status": response.status_code,
            "response_body": _short_body(response),
            "entitlement_hint": endpoint_hint(response.status_code, name),
        }

    @staticmethod
    def _rate_headers(response: requests.Response) -> Dict[str, Any]:
        return {
            "limit": response.headers.get("X-RateLimit-Limit"),
            "remaining": response.headers.get("X-RateLimit-Remaining"),
            "reset": response.headers.get("X-RateLimit-Reset"),
        }

    def get_assets(self) -> List[Dict[str, Any]]:
        for base in (self.options_contracts_base_url, self.paper_base_url, self.live_base_url):
            response = self._request("GET", base, "/v2/assets", params={"status": "active", "asset_class": "us_equity"})
            if response.status_code < 400:
                data = response.json()
                return data if isinstance(data, list) else []
        return []

    def get_option_contracts(
        self,
        *,
        expiration_gte: date,
        expiration_lte: date,
        underlying_symbols: Optional[List[str]] = None,
        limit: int = 10000,
        max_contracts: int = 10000,
    ) -> List[Dict[str, Any]]:
        contracts: List[Dict[str, Any]] = []
        token: Optional[str] = None
        while len(contracts) < max_contracts:
            params: Dict[str, Any] = {
                "status": "active",
                "expiration_date_gte": expiration_gte.isoformat(),
                "expiration_date_lte": expiration_lte.isoformat(),
                "limit": min(limit, max_contracts - len(contracts)),
            }
            if underlying_symbols:
                params["underlying_symbols"] = ",".join(underlying_symbols)
            if token:
                params["page_token"] = token
            response = self._request("GET", self.options_contracts_base_url, "/v2/options/contracts", params=params, timeout=45)
            if response.status_code >= 400:
                hint = endpoint_hint(response.status_code, "options contracts")
                raise RuntimeError(
                    "option contracts unavailable: "
                    f"{response.status_code} {response.text[:180]} "
                    f"url={self.endpoint_url(self.options_contracts_base_url, '/v2/options/contracts')} "
                    f"hint={hint}"
                )
            body = response.json()
            rows = body.get("option_contracts") or body.get("contracts") or body.get("data") or []
            contracts.extend([row for row in rows if isinstance(row, dict)])
            token = body.get("next_page_token")
            if not token or not rows:
                break
        return contracts[:max_contracts]

    def get_option_snapshots(self, option_symbols: List[str], *, feed: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        feed = (feed or self.options_feed).lower()
        snapshots: Dict[str, Dict[str, Any]] = {}
        for batch in chunked(option_symbols, 100):
            params = {"symbols": ",".join(batch), "feed": feed, "limit": 1000}
            response = self._request("GET", self.options_data_base_url, "/v1beta1/options/snapshots", params=params, timeout=45)
            if response.status_code >= 400 and feed == "opra" and self.allow_indicative_fallback:
                params["feed"] = "indicative"
                response = self._request("GET", self.options_data_base_url, "/v1beta1/options/snapshots", params=params, timeout=45)
            if response.status_code >= 400:
                raise RuntimeError(f"option snapshots unavailable: {response.status_code} {response.text[:180]}")
            body = response.json()
            raw = body.get("snapshots") or body
            if isinstance(raw, dict):
                for symbol, item in raw.items():
                    if isinstance(item, dict):
                        item.setdefault("data_source", params["feed"])
                        snapshots[symbol] = item
        return snapshots

    def get_latest_option_quotes(self, option_symbols: List[str], *, feed: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        feed = (feed or self.options_feed).lower()
        quotes: Dict[str, Dict[str, Any]] = {}
        for batch in chunked(option_symbols, 100):
            response = self._request(
                "GET",
                self.options_data_base_url,
                "/v1beta1/options/quotes/latest",
                params={"symbols": ",".join(batch), "feed": feed},
                timeout=30,
            )
            if response.status_code >= 400:
                continue
            raw = response.json().get("quotes", response.json())
            if isinstance(raw, dict):
                quotes.update({k: v for k, v in raw.items() if isinstance(v, dict)})
        return quotes

    def get_option_trades(self, option_symbols: List[str], *, start: datetime, end: datetime, feed: Optional[str] = None, limit: int = 10000) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for batch in chunked(option_symbols, 100):
            response = self._request(
                "GET",
                self.options_data_base_url,
                "/v1beta1/options/trades",
                params={"symbols": ",".join(batch), "start": _iso(start), "end": _iso(end), "limit": limit},
                timeout=45,
            )
            if response.status_code >= 400:
                continue
            raw = response.json().get("trades", {})
            if isinstance(raw, dict):
                for symbol, trades in raw.items():
                    out[symbol] = trades if isinstance(trades, list) else []
        return out

    def get_option_bars(self, option_symbols: List[str], *, start: datetime, end: datetime, timeframe: str = "1Min", feed: Optional[str] = None, limit: int = 10000) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for batch in chunked(option_symbols, 100):
            params = {"symbols": ",".join(batch), "timeframe": timeframe, "start": _iso(start), "end": _iso(end), "feed": (feed or self.options_feed).lower(), "limit": limit}
            response = self._request("GET", self.options_data_base_url, "/v1beta1/options/bars", params=params, timeout=45)
            if response.status_code >= 400 and params["feed"] == "opra" and self.allow_indicative_fallback:
                params["feed"] = "indicative"
                response = self._request("GET", self.options_data_base_url, "/v1beta1/options/bars", params=params, timeout=45)
            if response.status_code >= 400:
                continue
            raw = response.json().get("bars", {})
            if isinstance(raw, dict):
                for symbol, bars in raw.items():
                    out[symbol] = bars if isinstance(bars, list) else []
        return out

    def get_option_quotes(self, option_symbols: List[str], *, start: datetime, end: datetime, feed: Optional[str] = None, limit: int = 10000) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for batch in chunked(option_symbols, 100):
            params = {"symbols": ",".join(batch), "start": _iso(start), "end": _iso(end), "feed": (feed or self.options_feed).lower(), "limit": limit}
            response = self._request("GET", self.options_data_base_url, "/v1beta1/options/quotes", params=params, timeout=45)
            if response.status_code >= 400 and params["feed"] == "opra" and self.allow_indicative_fallback:
                params["feed"] = "indicative"
                response = self._request("GET", self.options_data_base_url, "/v1beta1/options/quotes", params=params, timeout=45)
            if response.status_code >= 400:
                continue
            raw = response.json().get("quotes", {})
            if isinstance(raw, dict):
                for symbol, quotes in raw.items():
                    out[symbol] = quotes if isinstance(quotes, list) else []
        return out

    def get_stock_bars(self, symbols: List[str], *, start: datetime, end: datetime, timeframe: str = "1Min") -> Dict[str, List[Dict[str, Any]]]:
        response = self._request(
            "GET",
            self.options_data_base_url,
            "/v2/stocks/bars",
            params={
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": _iso(start),
                "end": _iso(end),
                "feed": self.stock_feed,
                "limit": 10000,
            },
            timeout=45,
        )
        if response.status_code >= 400:
            return {}
        raw = response.json().get("bars", {})
        return {symbol: bars for symbol, bars in raw.items() if isinstance(bars, list)}

    def check_access(self) -> Dict[str, Any]:
        status = OptionsAccessStatus()
        status.contracts_url_used = self.endpoint_url(self.options_contracts_base_url, "/v2/options/contracts")
        status.data_url_used = self.options_data_base_url
        status.paper_or_live_mode = self.paper_or_live_mode
        if not self.api_key or not self.secret_key:
            status.last_error = "Alpaca API key/secret are not configured."
            return status.to_dict()
        today = datetime.now(timezone.utc).date()
        rate: Dict[str, Any] = {}
        diagnostics: Dict[str, Any] = {}
        try:
            stock = self._request("GET", self.options_data_base_url, "/v2/stocks/bars/latest", params={"symbols": "AAPL", "feed": self.stock_feed}, timeout=15)
            diagnostics["stock_latest_bar"] = self._diagnostic("stock_latest_bar", stock, self.options_data_base_url, "/v2/stocks/bars/latest")
            status.alpaca_connected = stock.status_code < 400
            rate = self._rate_headers(stock)
            contracts = self._request(
                "GET",
                self.options_contracts_base_url,
                "/v2/options/contracts",
                params={"status": "active", "expiration_date_gte": today.isoformat(), "expiration_date_lte": (today + timedelta(days=7)).isoformat(), "limit": 1},
                timeout=15,
            )
            diagnostics["contracts"] = self._diagnostic("contracts", contracts, self.options_contracts_base_url, "/v2/options/contracts")
            status.options_contracts_available = contracts.status_code < 400
            if contracts.status_code >= 400:
                hint = endpoint_hint(contracts.status_code, "contracts")
                status.last_error = f"contracts: {contracts.status_code} {contracts.text[:120]}"
                status.entitlement_hint = hint
                status.data_plan_warning = hint
            symbols: List[str] = []
            if contracts.status_code < 400:
                rows = contracts.json().get("option_contracts") or []
                for row in rows:
                    sym = row.get("symbol")
                    if sym:
                        symbols.append(sym)
            if symbols:
                snapshot = self._request("GET", self.options_data_base_url, "/v1beta1/options/snapshots", params={"symbols": symbols[0], "feed": self.options_feed}, timeout=15)
                diagnostics["snapshots"] = self._diagnostic("snapshots", snapshot, self.options_data_base_url, "/v1beta1/options/snapshots")
                status.options_snapshots_available = snapshot.status_code < 400
                status.official_options_feed_available = self.options_feed == "opra" and snapshot.status_code < 400
                if snapshot.status_code >= 400:
                    status.data_plan_warning = endpoint_hint(snapshot.status_code, "options snapshots")
                    status.entitlement_hint = status.data_plan_warning
                quote = self._request("GET", self.options_data_base_url, "/v1beta1/options/quotes/latest", params={"symbols": symbols[0], "feed": self.options_feed}, timeout=15)
                diagnostics["quotes_latest"] = self._diagnostic("quotes_latest", quote, self.options_data_base_url, "/v1beta1/options/quotes/latest")
                status.options_quotes_available = quote.status_code < 400
                start = datetime.now(timezone.utc) - timedelta(minutes=15)
                trades = self._request("GET", self.options_data_base_url, "/v1beta1/options/trades", params={"symbols": symbols[0], "start": _iso(start), "limit": 1}, timeout=15)
                diagnostics["trades"] = self._diagnostic("trades", trades, self.options_data_base_url, "/v1beta1/options/trades")
                status.options_trades_available = trades.status_code < 400
                bars = self._request("GET", self.options_data_base_url, "/v1beta1/options/bars", params={"symbols": symbols[0], "timeframe": "1Min", "start": _iso(start), "limit": 1}, timeout=15)
                diagnostics["bars"] = self._diagnostic("bars", bars, self.options_data_base_url, "/v1beta1/options/bars")
                status.options_bars_available = bars.status_code < 400
            status.rate_limit_status = rate
        except Exception as exc:
            status.last_error = str(exc)
        status.endpoint_diagnostics = diagnostics
        return status.to_dict()
