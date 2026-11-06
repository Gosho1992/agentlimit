from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class BudgetPolicy(Base):
    __tablename__ = "budget_policies"

    policy_id: Mapped[str] = mapped_column(String, primary_key=True)
    cpu_budget_usec: Mapped[int] = mapped_column(Integer, default=30_000_000)
    memory_limit_bytes: Mapped[int] = mapped_column(Integer, default=512 * 1024 * 1024)
    wall_clock_limit_seconds: Mapped[int] = mapped_column(Integer, default=300)
    token_limit: Mapped[int] = mapped_column(Integer, default=50_000)
    alert_pct: Mapped[int] = mapped_column(Integer, default=70)
    recommend_pct: Mapped[int] = mapped_column(Integer, default=80)
    throttle_pct: Mapped[int] = mapped_column(Integer, default=85)
    pause_pct: Mapped[int] = mapped_column(Integer, default=95)

    _DEFAULTS = {"cpu_budget_usec": 30_000_000, "memory_limit_bytes": 512 * 1024 * 1024,
                 "wall_clock_limit_seconds": 300, "token_limit": 50_000,
                 "alert_pct": 70, "recommend_pct": 80, "throttle_pct": 85, "pause_pct": 95}

    def __init__(self, **kw):
        # SQLAlchemy column defaults only apply on INSERT; apply them at
        # construction too so an un-persisted instance is never half-None.
        for k, v in self._DEFAULTS.items():
            kw.setdefault(k, v)
        super().__init__(**kw)


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, default="demo")
    sandbox_id: Mapped[str | None] = mapped_column(String, nullable=True)
    budget_policy_id: Mapped[str | None] = mapped_column(ForeignKey("budget_policies.policy_id"), nullable=True)
    task: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)