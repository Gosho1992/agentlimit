"""
Threshold state machine.

  - utilisation() returns per-metric percentages and which metric is worst.
  - target_state() returns the highest state the metrics justify.
  - path_to() returns the ordered states to walk UP through, so if metrics
    jump from 78% to 88% the engine still passes RECOMMEND before THROTTLE.
  - de-escalation (resume) is explicit: see GovernanceEngine.resume().
  - Memory is only considered once host-side memory telemetry is validated.
  - Dollar cost is only considered when a cost cap is configured.
"""

from enum import Enum


class State(str, Enum):
    NORMAL = "NORMAL"
    ALERT = "ALERT"
    RECOMMEND = "RECOMMEND"
    THROTTLE = "THROTTLE"
    PAUSE = "PAUSE"
    KILL = "KILL"


ORDER = [State.NORMAL, State.ALERT, State.RECOMMEND, State.THROTTLE, State.PAUSE, State.KILL]


def rank(s: State) -> int:
    return ORDER.index(s)


class Thresholds:
    @staticmethod
    def utilisation(
        cpu_usec: int,
        tokens: int,
        mem_peak_bytes: int,
        wall_seconds: float,
        memory_valid: bool,
        budget,
        cost_usd: float = 0.0,
    ) -> dict[str, float]:
        u = {
            "cpu": cpu_usec / max(budget.cpu_budget_usec, 1) * 100.0,
            "tokens": tokens / max(budget.token_limit, 1) * 100.0,
            "wall_clock": wall_seconds / max(budget.wall_clock_limit_seconds, 1) * 100.0,
        }
        if memory_valid:
            u["memory"] = mem_peak_bytes / max(budget.memory_limit_bytes, 1) * 100.0
        cost_cap = getattr(budget, "cost_limit_usd", 0.0) or 0.0
        if cost_cap > 0:
            u["cost"] = cost_usd / cost_cap * 100.0
        return u

    @staticmethod
    def state_for_pct(worst: float, budget) -> State:
        if worst >= 100.0:
            return State.KILL
        if worst >= budget.pause_pct:
            return State.PAUSE
        if worst >= budget.throttle_pct:
            return State.THROTTLE
        if worst >= budget.recommend_pct:
            return State.RECOMMEND
        if worst >= budget.alert_pct:
            return State.ALERT
        return State.NORMAL

    @staticmethod
    def target_state(
        cpu_usec: int,
        tokens: int,
        mem_peak_bytes: int,
        wall_seconds: float,
        memory_valid: bool,
        budget,
        cost_usd: float = 0.0,
    ) -> State:
        u = Thresholds.utilisation(cpu_usec, tokens, mem_peak_bytes, wall_seconds,
                                   memory_valid, budget, cost_usd)
        return Thresholds.state_for_pct(max(u.values()) if u else 0.0, budget)

    @staticmethod
    def path_to(current: State, target: State) -> list[State]:
        """States to transition through going UP (excludes current, includes
        target). Empty if target <= current."""
        i, j = rank(current), rank(target)
        if j <= i:
            return []
        return ORDER[i + 1: j + 1]
