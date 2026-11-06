from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class EvidenceArtifact(Base):
    __tablename__ = "evidence_artifacts"

    artifact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), index=True)
    file_name: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class VerificationResult(Base):
    __tablename__ = "verification_results"

    verification_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), index=True)
    verification_level: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String)                 # VERIFIED | INTEGRITY_ONLY | FAILED
    detail: Mapped[str] = mapped_column(Text, default="")
    signature: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)