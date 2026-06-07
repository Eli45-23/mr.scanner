from __future__ import annotations

from typing import List


def stage_from_score(
    score: int,
    *,
    confirmed: bool = False,
    good_position: bool = False,
    late: bool = False,
    do_not_chase: bool = False,
    invalidated: bool = False,
) -> str:
    if invalidated:
        return "INVALIDATED"
    if do_not_chase:
        return "DO_NOT_CHASE"
    if late or score >= 85:
        return "LATE" if late else "GOOD_POSITION" if good_position else "CONFIRMED"
    if good_position:
        return "GOOD_POSITION"
    if confirmed and score >= 70:
        return "CONFIRMED"
    if score >= 55:
        return "FORMING"
    return "WATCHING"


def entry_quality_from_stage(stage: str) -> str:
    if stage in {"GOOD_POSITION", "CONFIRMED"}:
        return "GOOD_POSITION" if stage == "GOOD_POSITION" else "EARLY"
    if stage in {"LATE", "DO_NOT_CHASE", "INVALIDATED"}:
        return stage
    if stage == "FORMING":
        return "EARLY"
    return "UNKNOWN"


def risk_from_stage(stage: str, warnings: List[str]) -> str:
    warning_text = " ".join(warnings).lower()
    if stage == "DO_NOT_CHASE" or "do not chase" in warning_text:
        return "DO_NOT_CHASE"
    if stage == "LATE" or "fakeout" in warning_text or "exhaust" in warning_text:
        return "HIGH"
    if stage == "CONFIRMED":
        return "MEDIUM"
    if stage == "GOOD_POSITION":
        return "LOW"
    return "MEDIUM"
