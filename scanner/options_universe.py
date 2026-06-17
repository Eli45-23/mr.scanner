from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class UniverseEntry:
    underlying_symbol: str
    asset_name: Optional[str]
    optionable: bool
    active: bool
    tradable: bool
    last_seen: str
    contract_count: int
    expirations_available: List[str]
    source: str


def default_universe_path(root: Optional[Path] = None) -> Path:
    return (root or Path.cwd()) / "data" / "options_universe.json"


def load_universe_cache(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"entries": [], "status": "missing", "warning": "Universe cache not built yet."}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"entries": [], "status": "error", "warning": f"Universe cache unreadable: {exc}"}


def write_universe_cache(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def build_optionable_universe(
    client: Any,
    config: Dict[str, Any],
    *,
    today: Optional[date] = None,
    cache_path: Optional[Path] = None,
) -> Dict[str, Any]:
    today = today or date.today()
    whale = config.get("options_whale_scanner", {})
    max_dte = int(whale.get("max_dte", 7))
    max_contracts = int(whale.get("max_contracts_per_scan", 10000))
    warning = ""
    assets_by_symbol: Dict[str, Dict[str, Any]] = {}
    try:
        assets_by_symbol = {
            str(asset.get("symbol") or "").upper(): asset
            for asset in client.get_assets()
            if asset.get("symbol")
        }
    except Exception:
        assets_by_symbol = {}
    try:
        contracts = client.get_option_contracts(
            expiration_gte=today,
            expiration_lte=today + timedelta(days=max_dte),
            limit=10000,
            max_contracts=max_contracts,
        )
        source = "alpaca_options_contracts"
    except Exception as exc:
        cached = load_universe_cache(cache_path or default_universe_path())
        cached["status"] = "fallback_cache"
        cached["warning"] = f"Full-market discovery partial — using cached optionable universe. {exc}"
        return cached

    by_underlying: Dict[str, Dict[str, Any]] = {}
    for contract in contracts:
        underlying = str(
            contract.get("underlying_symbol")
            or contract.get("underlying_asset_symbol")
            or contract.get("root_symbol")
            or ""
        ).upper()
        if not underlying:
            # OCC symbols usually begin with the root symbol before YYMMDD.
            symbol = str(contract.get("symbol") or "")
            underlying = symbol.split("2", 1)[0].strip().upper()
        if not underlying:
            continue
        record = by_underlying.setdefault(underlying, {"count": 0, "expirations": set()})
        record["count"] += 1
        exp = contract.get("expiration_date") or contract.get("expiration")
        if exp:
            record["expirations"].add(str(exp)[:10])
    entries: List[Dict[str, Any]] = []
    for symbol, item in sorted(by_underlying.items()):
        asset = assets_by_symbol.get(symbol, {})
        entries.append(asdict(UniverseEntry(
            underlying_symbol=symbol,
            asset_name=asset.get("name"),
            optionable=True,
            active=asset.get("status", "active") == "active",
            tradable=bool(asset.get("tradable", True)),
            last_seen=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            contract_count=int(item["count"]),
            expirations_available=sorted(item["expirations"]),
            source=source,
        )))
    payload = {
        "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "status": "ok",
        "warning": warning,
        "full_market_discovery": True,
        "entry_count": len(entries),
        "contract_count": len(contracts),
        "entries": entries,
    }
    if cache_path:
        write_universe_cache(cache_path, payload)
    return payload


def universe_status(path: Path) -> Dict[str, Any]:
    payload = load_universe_cache(path)
    return {
        "status": payload.get("status", "unknown"),
        "generated_at": payload.get("generated_at"),
        "entry_count": payload.get("entry_count", len(payload.get("entries") or [])),
        "contract_count": payload.get("contract_count"),
        "warning": payload.get("warning", ""),
        "source": "cache",
    }
