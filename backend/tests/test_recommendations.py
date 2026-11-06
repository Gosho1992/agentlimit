from backend.governance.recommendation_engine import RecommendationEngine


def test_retry_loop_fires_at_three():
    eng = RecommendationEngine()
    recs = eng.evaluate({
        "memory_telemetry_valid": False,
        "consecutive_identical_errors": 3,
    })
    assert len(recs) == 1
    assert recs[0].rule_id == "retry_loop"
    assert recs[0].auto_applicable is True


def test_retry_loop_does_not_fire_below_three():
    eng = RecommendationEngine()
    recs = eng.evaluate({
        "memory_telemetry_valid": False,
        "consecutive_identical_errors": 2,
    })
    assert recs == []


def test_context_growth_fires_when_last_exceeds_median():
    eng = RecommendationEngine()
    recs = eng.evaluate({
        "memory_telemetry_valid": False,
        "tokens_last_step": 3000,
        "tokens_median": 1000,
    })
    assert len(recs) == 1
    assert recs[0].rule_id == "context_growth"


def test_memory_rule_requires_valid_telemetry():
    eng = RecommendationEngine()
    # even with memory near the cap, no recommendation if telemetry not validated
    recs = eng.evaluate({
        "memory_telemetry_valid": False,
        "memory_current": 500 * 1024 * 1024,
        "memory_max": 512 * 1024 * 1024,
    })
    assert recs == []


def test_memory_rule_fires_when_telemetry_validated():
    eng = RecommendationEngine()
    recs = eng.evaluate({
        "memory_telemetry_valid": True,
        "memory_current": 500 * 1024 * 1024,
        "memory_max": 512 * 1024 * 1024,
    })
    assert len(recs) == 1
    assert recs[0].rule_id == "memory_pressure"