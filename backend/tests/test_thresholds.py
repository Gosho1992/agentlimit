import pytest

from backend.governance.budget import SessionBudget
from backend.governance.thresholds import State, Thresholds


def B(**kw):
    return SessionBudget(policy_id="t", **kw).validate()


def ts(cpu=0, tokens=0, mem=0, wall=0.0, mem_valid=False, cost=0.0, budget=None):
    return Thresholds.target_state(cpu, tokens, mem, wall, mem_valid, budget or B(), cost)


@pytest.mark.parametrize("cpu,expected", [
    (0, State.NORMAL), (20_000_000, State.NORMAL), (21_000_000, State.ALERT),
    (24_000_000, State.RECOMMEND), (25_500_000, State.THROTTLE), (28_500_000, State.PAUSE),
    (30_000_000, State.KILL), (35_000_000, State.KILL),
])
def test_cpu_state_ladder(cpu, expected):
    assert ts(cpu=cpu) == expected


def test_tokens_drive_state_independent_of_cpu():
    assert ts(tokens=40_000) == State.RECOMMEND


def test_memory_ignored_until_validated_then_drives_state():
    mem = int(0.9 * 512 * 2**20)
    assert ts(mem=mem, mem_valid=False) == State.NORMAL
    assert ts(mem=mem, mem_valid=True) == State.THROTTLE


def test_wall_clock_enforced():
    assert ts(wall=300) == State.KILL


def test_cost_cap_only_when_configured():
    assert ts(cost=5.0) == State.NORMAL
    assert ts(cost=0.96, budget=B(cost_limit_usd=1.0)) == State.PAUSE


def test_most_restrictive_wins():
    assert ts(cpu=27_000_000, tokens=50_000) == State.KILL


def test_path_walks_every_intermediate_state():
    assert Thresholds.path_to(State.ALERT, State.PAUSE) == [State.RECOMMEND, State.THROTTLE, State.PAUSE]
    assert Thresholds.path_to(State.PAUSE, State.ALERT) == []
