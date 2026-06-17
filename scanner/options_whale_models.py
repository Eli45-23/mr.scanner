from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


DISCLAIMER = "Possible whale flow — not a trade signal."


@dataclass
class OptionContractRecord:
    underlying_symbol: str
    option_symbol: str
    option_type: str
    expiration: str
    strike: float
    contract_id: Optional[str] = None
    asset_name: Optional[str] = None
    source: str = "alpaca"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OptionFlowCandidate:
    time_detected: str
    underlying_symbol: str
    underlying_price: Optional[float]
    option_symbol: str
    contract_id: Optional[str]
    option_type: str
    expiration: str
    dte: int
    strike: float
    moneyness: str
    distance_from_underlying_price: Optional[float]
    distance_percent: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    midpoint: Optional[float]
    spread: Optional[float]
    spread_percent: Optional[float]
    volume: int
    open_interest: Optional[int]
    volume_oi_ratio: Optional[float]
    previous_open_interest: Optional[int] = None
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    trade_count: Optional[int] = None
    quote_time: Optional[str] = None
    trade_time: Optional[str] = None
    quote_freshness_seconds: Optional[float] = None
    estimated_premium: float = 0.0
    data_source: str = "alpaca"
    warnings: List[str] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WhaleFlowResult:
    candidate: Dict[str, Any]
    whale_score: int
    score_components: Dict[str, Any]
    classification: str
    reason_summary: str
    detailed_reasons: List[str]
    aggression_side: str
    aggression_score: int
    direction_label: str
    direction_confidence: str
    direction_warning: str
    is_possible_sweep: bool = False
    sweep_group_id: Optional[str] = None
    sweep_trade_count: int = 0
    sweep_total_volume: int = 0
    sweep_total_premium: float = 0.0
    sweep_time_window_seconds: Optional[float] = None
    sweep_reason: str = ""
    is_possible_block: bool = False
    block_size: Optional[int] = None
    block_premium: Optional[float] = None
    block_reason: str = ""
    possible_multileg: bool = False
    multileg_type: str = "none"
    linked_contracts: List[str] = field(default_factory=list)
    direction_clarity: str = "unclear"
    multileg_warning: str = ""
    opening_flow_estimate: str = "unknown"
    opening_confidence: str = "LOW"
    oi_warning: str = ""
    price_context: Dict[str, Any] = field(default_factory=dict)
    price_context_score: int = 0
    price_confirmation_label: str = "needs price confirmation"
    price_warning: str = ""
    alert_tier: str = "IGNORE"
    should_notify: bool = False
    notify_reason: str = ""
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_expiration(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
