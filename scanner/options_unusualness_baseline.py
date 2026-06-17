from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bucket_dte(value: Any) -> str:
    dte = _safe_int(value, 999)
    if dte <= 0:
        return "0DTE"
    if dte <= 2:
        return "1-2DTE"
    if dte <= 7:
        return "3-7DTE"
    if dte <= 30:
        return "8-30DTE"
    return "31DTE+"


def _bucket_moneyness(value: Any) -> str:
    text = str(value or "UNKNOWN").upper()
    return text if text in {"ATM", "ITM", "OTM"} else "UNKNOWN"


def _z_score(value: float, avg: float, stdev: float) -> float:
    if stdev <= 0:
        if avg <= 0 and value > 0:
            return 3.0
        if avg > 0 and value > avg:
            return min(3.0, value / avg - 1.0)
        return 0.0
    return (value - avg) / stdev


def _percentile_like(value: float, samples: List[float]) -> float:
    clean = sorted(v for v in samples if v >= 0)
    if not clean:
        return 0.0
    below = sum(1 for item in clean if item <= value)
    return round(100.0 * below / len(clean), 2)


@dataclass
class BaselineStats:
    sample_size: int
    average_volume: float
    average_premium: float
    volume_stdev: float
    premium_stdev: float
    volume_samples: List[float]
    premium_samples: List[float]

    @classmethod
    def from_records(cls, records: Iterable[Dict[str, Any]]) -> "BaselineStats":
        volumes: List[float] = []
        premiums: List[float] = []
        for record in records:
            candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else record
            volumes.append(_safe_float(candidate.get("volume")))
            premiums.append(_safe_float(candidate.get("estimated_premium") or candidate.get("premium")))
        return cls(
            sample_size=len(volumes),
            average_volume=round(mean(volumes), 2) if volumes else 0.0,
            average_premium=round(mean(premiums), 2) if premiums else 0.0,
            volume_stdev=round(pstdev(volumes), 2) if len(volumes) > 1 else 0.0,
            premium_stdev=round(pstdev(premiums), 2) if len(premiums) > 1 else 0.0,
            volume_samples=volumes[-500:],
            premium_samples=premiums[-500:],
        )

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "average_volume": self.average_volume,
            "average_premium": self.average_premium,
            "volume_stdev": self.volume_stdev,
            "premium_stdev": self.premium_stdev,
        }


class OptionsUnusualnessBaseline:
    """Local historical baseline for professional options-flow scoring.

    This is intentionally local/read-only. It learns from prior scan observations and
    answers whether a new candidate is unusual for its symbol, DTE bucket, and
    moneyness bucket. It does not call order endpoints or infer certainty.
    """

    def __init__(self, root: Path, *, max_records: int = 25000) -> None:
        self.root = Path(root)
        self.path = self.root / "data" / "options_unusualness_baseline.jsonl"
        self.max_records = max(100, int(max_records))

    def _candidate_record(self, row: Dict[str, Any]) -> Dict[str, Any]:
        candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else row
        return {
            "timestamp": row.get("timestamp") or candidate.get("time_detected"),
            "underlying_symbol": str(candidate.get("underlying_symbol") or candidate.get("underlying") or "").upper(),
            "option_symbol": candidate.get("option_symbol") or candidate.get("contract_symbol"),
            "option_type": candidate.get("option_type"),
            "dte": _safe_int(candidate.get("dte") if candidate.get("dte") is not None else candidate.get("days_to_expiration"), 999),
            "dte_bucket": _bucket_dte(candidate.get("dte") if candidate.get("dte") is not None else candidate.get("days_to_expiration")),
            "moneyness": _bucket_moneyness(candidate.get("moneyness")),
            "volume": _safe_float(candidate.get("volume")),
            "open_interest": _safe_float(candidate.get("open_interest")),
            "volume_oi_ratio": _safe_float(candidate.get("volume_oi_ratio")),
            "estimated_premium": _safe_float(candidate.get("estimated_premium") or candidate.get("premium")),
            "spread_percent": _safe_float(candidate.get("spread_percent")),
        }

    def load_records(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in lines[-self.max_records:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                records.append(row)
        return records

    def append_observations(self, rows: Iterable[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        records = [self._candidate_record(row) for row in rows]
        records = [row for row in records if row.get("underlying_symbol") and row.get("option_symbol")]
        if not records:
            return
        try:
            existing = self.load_records()
            combined = (existing + records)[-self.max_records:]
            self.path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in combined) + "\n", encoding="utf-8")
        except OSError:
            return

    def evaluate_candidate(self, candidate: Dict[str, Any], records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        records = records if records is not None else self.load_records()
        symbol = str(candidate.get("underlying_symbol") or "").upper()
        dte_bucket = _bucket_dte(candidate.get("dte") if candidate.get("dte") is not None else candidate.get("days_to_expiration"))
        moneyness = _bucket_moneyness(candidate.get("moneyness"))
        option_type = str(candidate.get("option_type") or "").upper()
        volume = _safe_float(candidate.get("volume"))
        premium = _safe_float(candidate.get("estimated_premium") or candidate.get("premium"))

        symbol_records = [row for row in records if str(row.get("underlying_symbol") or "").upper() == symbol]
        bucket_records = [
            row for row in symbol_records
            if row.get("dte_bucket") == dte_bucket and row.get("moneyness") == moneyness and str(row.get("option_type") or "").upper() == option_type
        ]
        market_bucket_records = [
            row for row in records
            if row.get("dte_bucket") == dte_bucket and row.get("moneyness") == moneyness and str(row.get("option_type") or "").upper() == option_type
        ]
        baseline_records = bucket_records or symbol_records or market_bucket_records
        stats = BaselineStats.from_records(baseline_records)
        volume_z = round(_z_score(volume, stats.average_volume, stats.volume_stdev), 2)
        premium_z = round(_z_score(premium, stats.average_premium, stats.premium_stdev), 2)
        volume_pct = _percentile_like(volume, stats.volume_samples)
        premium_pct = _percentile_like(premium, stats.premium_samples)

        score = 0
        score += min(8, max(0, int(volume_z * 2)))
        score += min(8, max(0, int(premium_z * 2)))
        if volume_pct >= 95:
            score += 5
        elif volume_pct >= 85:
            score += 3
        if premium_pct >= 95:
            score += 5
        elif premium_pct >= 85:
            score += 3
        if stats.sample_size < 20:
            score = min(score, 8)
        score = max(0, min(20, score))

        warnings: List[str] = []
        if stats.sample_size < 20:
            warnings.append("limited historical baseline; unusualness confidence is reduced")
        if symbol in {"SPY", "QQQ", "IWM"} and dte_bucket == "0DTE" and score < 10:
            warnings.append("index 0DTE flow may be normal noise without stronger unusualness")

        label = "EXTREME_UNUSUAL" if score >= 17 else "HIGHLY_UNUSUAL" if score >= 13 else "UNUSUAL" if score >= 8 else "NORMAL_OR_UNCONFIRMED"
        return {
            "unusualness_score": score,
            "unusualness_label": label,
            "unusualness_sample_size": stats.sample_size,
            "unusualness_baseline": stats.to_public_dict(),
            "volume_z_score": volume_z,
            "premium_z_score": premium_z,
            "volume_percentile": volume_pct,
            "premium_percentile": premium_pct,
            "dte_bucket": dte_bucket,
            "baseline_scope": "symbol_dte_moneyness" if bucket_records else "symbol" if symbol_records else "market_bucket" if market_bucket_records else "empty",
            "unusualness_warnings": warnings,
        }
