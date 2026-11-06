"""
Runtime session budget.

This is a plain dataclass with explicit defaults, deliberately NOT an ORM
object. SQLAlchemy `default=` values are only applied at INSERT time, so an
un-persisted ORM instance has None for every unset column — which made the
threshold comparisons raise TypeError on every governance tick.

validate() is called before a session starts; a session never runs with a
missing or inconsistent threshold.
"""

from dataclasses import asdict, dataclass


@dataclass
class SessionBudget:
    policy_id: str
    cpu_budget_usec: int = 30_000_000          # 30 CPU-seconds (cumulative, host-side)
    memory_limit_bytes: int = 512 * 1024 * 1024
    wall_clock_limit_seconds: int = 300
    token_limit: int = 50_000
    cost_limit_usd: float = 0.0                # 0 = no dollar cap (estimated cost only)
    alert_pct: int = 70
    recommend_pct: int = 80
    throttle_pct: int = 85
    pause_pct: int = 95

    def validate(self) -> "SessionBudget":
        for name in ("cpu_budget_usec", "memory_limit_bytes",
                     "wall_clock_limit_seconds", "token_limit"):
            v = getattr(self, name)
            if not isinstance(v, int) or v <= 0:
                raise ValueError(f"budget.{name} must be a positive int, got {v!r}")
        if self.cost_limit_usd is None or self.cost_limit_usd < 0:
            raise ValueError("budget.cost_limit_usd must be >= 0")
        ladder = [self.alert_pct, self.recommend_pct, self.throttle_pct, self.pause_pct]
        if any(not isinstance(p, int) for p in ladder):
            raise ValueError(f"threshold percentages must all be ints, got {ladder}")
        if not (0 < self.alert_pct < self.recommend_pct < self.throttle_pct < self.pause_pct < 100):
            raise ValueError(
                "thresholds must satisfy 0 < alert < recommend < throttle < pause < 100, "
                f"got {ladder}"
            )
        return self

    def to_dict(self) -> dict:
        return asdict(self)
