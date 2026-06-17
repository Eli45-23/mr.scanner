from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests


DATA_BASE = "https://data.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
READ_ONLY_PATHS = (
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
        }


def _iso(dt: date | datetime) -> str:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return dt.isoformat()


def chunked(values: List[str], size: int) -> Iterable[List[str]]:
    for idx in range(0, len(values), size):
        yield values[idx: idx + size]


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
    ) -> None:
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self.stock_feed = (stock_feed or "sip").lower()
        self.options_feed = (options_feed or "opra").lower()
        self.allow_indicative_fallback = allow_indicative_fallback
        self.session = session or requests.Session()
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

    @staticmethod
    def _rate_headers(response: requests.Response) -> Dict[str, Any]:
        return {
            "limit": response.headers.get("X-RateLimit-Limit"),
            "remaining": response.headers.get("X-RateLimit-Remaining"),
            "reset": response.headers.get("X-RateLimit-Reset"),
        }

    def get_assets(self) -> List[Dict[str, Any]]:
        for base in (PAPER_BASE, LIVE_BASE):
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
            response = self._request("GET", PAPER_BASE, "/v2/options/contracts", params=params, timeout=45)
            if response.status_code >= 400:
                raise RuntimeError(f"option contracts unavailable: {response.status_code} {response.text[:180]}")
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
            response = self._request("GET", DATA_BASE, "/v1beta1/options/snapshots", params=params, timeout=45)
            if response.status_code >= 400 and feed == "opra" and self.allow_indicative_fallback:
                params["feed"] = "indicative"
                response = self._request("GET", DATA_BASE, "/v1beta1/options/snapshots", params=params, timeout=45)
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
                DATA_BASE,
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
        feed = (feed or self.options_feed).lower()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for batch in chunked(option_symbols, 100):
            response = self._request(
                "GET",
                DATA_BASE,
                "/v1beta1/options/trades",
                params={"symbols": ",".join(batch), "feed": feed, "start": _iso(start), "end": _iso(end), "limit": limit},
                timeout=45,
            )
            if response.status_code >= 400:
                continue
            raw = response.json().get("trades", {})
            if isinstance(raw, dict):
                for symbol, trades in raw.items():
                    out[symbol] = trades if isinstance(trades, list) else []
        return out

    def get_stock_bars(self, symbols: List[str], *, start: datetime, end: datetime, timeframe: str = "1Min") -> Dict[str, List[Dict[str, Any]]]:
        response = self._request(
            "GET",
            DATA_BASE,
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
        if not self.api_key or not self.secret_key:
            status.last_error = "Alpaca API key/secret are not configured."
            return status.to_dict()
        today = datetime.now(timezone.utc).date()
        rate: Dict[str, Any] = {}
        try:
            stock = self._request("GET", DATA_BASE, "/v2/stocks/bars/latest", params={"symbols": "AAPL", "feed": self.stock_feed}, timeout=15)
            status.alpaca_connected = stock.status_code < 400
            rate = self._rate_headers(stock)
            contracts = self._request(
                "GET",
                PAPER_BASE,
                "/v2/options/contracts",
                params={"status": "active", "expiration_date_gte": today.isoformat(), "expiration_date_lte": (today + timedelta(days=7)).isoformat(), "limit": 1},
                timeout=15,
            )
            status.options_contracts_available = contracts.status_code < 400
            if contracts.status_code >= 400:
                status.last_error = f"contracts: {contracts.status_code} {contracts.text[:120]}"
            symbols: List[str] = []
            if contracts.status_code < 400:
                rows = contracts.json().get("option_contracts") or []
                for row in rows:
                    sym = row.get("symbol")
                    if sym:
                        symbols.append(sym)
            if symbols:
                snapshot = self._request("GET", DATA_BASE, "/v1beta1/options/snapshots", params={"symbols": symbols[0], "feed": self.options_feed}, timeout=15)
                status.options_snapshots_available = snapshot.status_code < 400
                status.official_options_feed_available = self.options_feed == "opra" and snapshot.status_code < 400
                if snapshot.status_code == 403:
                    status.data_plan_warning = "Alpaca options data unavailable or not enabled for this account."
                quote = self._request("GET", DATA_BASE, "/v1beta1/options/quotes/latest", params={"symbols": symbols[0], "feed": self.options_feed}, timeout=15)
                status.options_quotes_available = quote.status_code < 400
                start = datetime.now(timezone.utc) - timedelta(minutes=15)
                trades = self._request("GET", DATA_BASE, "/v1beta1/options/trades", params={"symbols": symbols[0], "feed": self.options_feed, "start": _iso(start), "limit": 1}, timeout=15)
                status.options_trades_available = trades.status_code < 400
                bars = self._request("GET", DATA_BASE, "/v1beta1/options/bars", params={"symbols": symbols[0], "feed": self.options_feed, "timeframe": "1Min", "start": _iso(start), "limit": 1}, timeout=15)
                status.options_bars_available = bars.status_code < 400
            status.rate_limit_status = rate
        except Exception as exc:
            status.last_error = str(exc)
        return status.to_dict()
