from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ScenarioResult:
    scenario_name: str
    direction: str = "neutral"
    score: int = 0
    stage: str = "WATCHING"
    confidence_label: str = "LOW"
    entry_quality_label: str = "UNKNOWN"
    risk_label: str = "MEDIUM"
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    invalidation_level: Optional[float] = None
    invalidation_reason: str = ""
    levels: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "direction": self.direction,
            "score": self.score,
            "stage": self.stage,
            "confidence_label": self.confidence_label,
            "entry_quality_label": self.entry_quality_label,
            "risk_label": self.risk_label,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "invalidation_level": self.invalidation_level,
            "invalidation_reason": self.invalidation_reason,
            "levels": self.levels,
        }
