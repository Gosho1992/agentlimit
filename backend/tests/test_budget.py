import pytest

from backend.api.sessions import CreateSessionRequest, build_budget
from backend.governance.budget import SessionBudget
from backend.models.session import BudgetPolicy


def test_api_budget_has_every_threshold_populated():
    b = build_budget(CreateSessionRequest(task="x"))
    for f in ("cpu_budget_usec", "memory_limit_bytes", "wall_clock_limit_seconds", "token_limit",
              "alert_pct", "recommend_pct", "throttle_pct", "pause_pct"):
        assert isinstance(getattr(b, f), int), f
    assert (b.alert_pct, b.recommend_pct, b.throttle_pct, b.pause_pct) == (70, 80, 85, 95)


def test_orm_budget_policy_no_longer_has_none_thresholds():
    # Regression: SQLAlchemy defaults are INSERT-time only; this used to be None.
    p = BudgetPolicy(policy_id="x", cpu_budget_usec=1)
    assert p.pause_pct == 95 and p.alert_pct == 70


@pytest.mark.parametrize("kw", [
    {"alert_pct": 90, "recommend_pct": 80},
    {"pause_pct": 100},
    {"cpu_budget_usec": 0},
    {"pause_pct": None},
])
def test_invalid_budgets_rejected(kw):
    with pytest.raises(ValueError):
        SessionBudget(policy_id="x", **kw).validate()
