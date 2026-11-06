from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class ExecutionStep(Base):
    __tablename__ = "execution_steps"

    step_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), index=True)
    step_number: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(String, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_snippet: Mapped[str] = mapped_column(String, default="")
    stderr_snippet: Mapped[str] = mapped_column(String, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResourceSample(Base):
    __tablename__ = "resource_samples"

    sample_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    cpu_usage_usec: Mapped[int] = mapped_column(Integer, default=0)
    memory_current_bytes: Mapped[int] = mapped_column(Integer, default=0)
    memory_peak_bytes: Mapped[int] = mapped_column(Integer, default=0)
    measurement_source: Mapped[str] = mapped_column(String, default="host_cgroup")