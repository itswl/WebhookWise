"""Persistence for incident intelligence inputs and operator feedback."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class ChangeEvent(Base):
    """A normalized deployment or configuration change used for correlation."""

    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    change_type: Mapped[str] = mapped_column(String(40), nullable=False)
    project: Mapped[str | None] = mapped_column(String(200))
    environment: Mapped[str | None] = mapped_column(String(200))
    service: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(200))
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(200))
    version_from: Mapped[str | None] = mapped_column(String(200))
    version_to: Mapped[str | None] = mapped_column(String(200))
    actor: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_url: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: utcnow(),
        onupdate=lambda: utcnow(),
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_change_events_source_external_id"),
        Index("ix_change_events_service_environment_started", "service", "environment", "started_at"),
        Index("ix_change_events_project_region_started", "project", "region", "started_at"),
    )


class IncidentIntelligenceFeedback(Base):
    """Operator feedback for a suggested incident, change, or runbook."""

    __tablename__ = "incident_intelligence_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recommendation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    candidate_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: utcnow(),
        onupdate=lambda: utcnow(),
    )

    __table_args__ = (
        UniqueConstraint(
            "incident_id",
            "recommendation_type",
            "candidate_ref",
            name="uq_incident_intelligence_feedback_candidate",
        ),
        Index(
            "ix_incident_intelligence_feedback_lookup",
            "incident_id",
            "recommendation_type",
        ),
    )
