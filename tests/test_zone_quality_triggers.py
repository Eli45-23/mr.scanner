from __future__ import annotations

from scanner.zone_quality import label_zone_quality, rank_zones_by_quality, score_zone_quality
from scanner.zone_triggers import derive_triggers_for_zones, derive_zone_triggers


def zone(**overrides):
    value = {
        "zone_type": "demand",
        "zone_low": 100.0,
        "zone_high": 100.4,
        "precision_zone_low": 100.1,
        "precision_zone_high": 100.3,
        "major_zone_low": 100.0,
        "major_zone_high": 100.4,
        "best_reaction_level": 100.22,
        "reaction_score": 90,
        "volume_score": 85,
        "freshness_score": 100,
        "impulse_score": 90,
        "times_tested": 0,
        "fresh": True,
        "width_bps": 20,
        "score": 85,
    }
    value.update(overrides)
    return value


def test_strong_fresh_impulse_zone_becomes_a_or_a_plus():
    result = score_zone_quality(zone())
    assert result["quality_label"] in {"A Zone", "A+ Zone"}
    assert result["is_dashboard_primary"] is False
    assert result["can_approve_trades"] is False


def test_weak_zone_becomes_weak_zone():
    result = score_zone_quality(zone(reaction_score=20, volume_score=10, freshness_score=40, impulse_score=10, times_tested=1))
    assert result["quality_label"] == "Weak Zone"


def test_too_wide_old_and_tapped_zones_are_downgraded():
    assert score_zone_quality(zone(too_wide=True))["quality_label"] == "Too Wide"
    assert score_zone_quality(zone(times_tested=5, freshness_score=10))["quality_label"] == "Old Zone"
    assert score_zone_quality(zone(times_tested=2, freshness_score=55))["quality_label"] == "Already Tapped"


def test_confluence_boosts_score():
    candidate = zone(reaction_score=65, volume_score=55, freshness_score=70, impulse_score=60, times_tested=1)
    plain = score_zone_quality(candidate)
    confluence = score_zone_quality(candidate, {"known_levels": {"pdl": 100.2, "vwap": 100.25}})
    assert confluence["quality_score"] > plain["quality_score"]
    assert any("Confluence" in reason for reason in confluence["quality_reasons"])


def test_ranking_puts_best_first_and_limits_dashboard_primary():
    ranked = rank_zones_by_quality(
        [
            zone(reaction_score=25, volume_score=20, freshness_score=20, impulse_score=20, times_tested=3),
            zone(reaction_score=95, volume_score=95, freshness_score=100, impulse_score=95),
            zone(reaction_score=80, volume_score=75, freshness_score=90, impulse_score=80),
        ],
        config={"max_primary_zones_per_type": 1},
    )
    assert ranked[0]["quality_score"] >= ranked[1]["quality_score"] >= ranked[2]["quality_score"]
    assert sum(item["is_dashboard_primary"] for item in ranked) == 1


def test_supply_zone_gets_trigger_rejection_and_outside_invalidation():
    result = derive_zone_triggers(zone(zone_type="supply"), current_price=99.5, atr=1.0)
    assert 100.1 <= result["trigger_level"] <= 100.3
    assert 100.1 <= result["rejection_line"] <= 100.3
    assert result["reclaim_line"] is None
    assert result["invalidation_level"] > 100.3


def test_demand_zone_gets_trigger_reclaim_and_outside_invalidation():
    result = derive_zone_triggers(zone(), current_price=101.0, atr=1.0)
    assert 100.1 <= result["trigger_level"] <= 100.3
    assert 100.1 <= result["reclaim_line"] <= 100.3
    assert result["rejection_line"] is None
    assert result["invalidation_level"] < 100.1


def test_atr_buffer_and_missing_atr_both_work():
    with_atr = derive_zone_triggers(zone(), atr=2.0)
    without_atr = derive_zone_triggers(zone(), atr=None)
    assert with_atr["invalidation_level"] < without_atr["invalidation_level"]
    assert without_atr["invalidation_level"] < 100.1


def test_trigger_enrichment_is_context_only_and_creates_no_notification_fields():
    enriched = derive_triggers_for_zones([zone()], current_price=101.0)
    assert enriched[0]["context_only"] is True
    assert enriched[0]["can_approve_trades"] is False
    assert "telegram_sent" not in enriched[0]
    assert "alert_type" not in enriched[0]


def test_label_helper_respects_thresholds():
    assert label_zone_quality({"quality_score": 90, "times_tested": 0}) == "A+ Zone"
