from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class GovernanceEvent(Base):
    __tablename__ = "governance_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), index=True)
    event_type: Mapped[str] = mapped_column(String)          # state_transition | throttle | pause | kill | resume
    from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    to_state: Mapped[str | None] = mapped_column(String, nullable=True)
    triggering_metric: Mapped[str] = mapped_column(String, default="")
    observed_value: Mapped[int] = mapped_column(Integer, default=0)
    policy_threshold: Mapped[int] = mapped_column(Integer, default=0)
    enforcement_action: Mapped[str] = mapped_column(String, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RecommendationRow(Base):
    __tablename__ = "recommendations"

    recommendation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), index=True)
    rule_id: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    metric: Mapped[str] = mapped_column(String)
    observed: Mapped[int] = mapped_column(Integer, default=0)
    threshold: Mapped[int] = mapped_column(Integer, default=0)
    suggestion: Mapped[str] = mapped_column(String, default="")
    action: Mapped[str] = mapped_column(String, default="")
    auto_applicable: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)