"""Incident model — groups related alerts into an operational incident."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class Incident(Base):
    """An operational incident formed by grouping related webhook alerts.

    The periodic grouping task scans recent events and clusters them by source +
    time proximity. Each incident stays *active* while new related alerts keep
    arriving; after a configurable quiet window it transitions to *quiet* and a
    one-shot LLM summary is generated.

    Membership is normalized in ``incident_members``. ``alert_count`` remains
    denormalized so list queries do not need an aggregate on every request.
    """

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False, index=True
    )  # active | quiet | closed

    source: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)

    alert_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top_importance: Mapped[str | None] = mapped_column(String(20))

    workflow_status: Mapped[str] = mapped_column(String(20), default="open", server_default="open", nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(100))
    team: Mapped[str | None] = mapped_column(String(100))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # Set when the SLA-breach escalation notification is queued, so the breach
    # is visible on the incident (dashboard badge / postmortem timeline) and
    # queryable without joining the outbox.
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime)

    correlation_dimensions: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'"),
        nullable=False,
    )
    correlation_confidence: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        server_default=text("0"),
        nullable=False,
    )

    # LLM-generated summary when the incident closes (null while active).
    summary_analysis: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    summary_status: Mapped[str | None] = mapped_column(String(20))
    summary_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    summary_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary_last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow(), onupdate=lambda: utcnow())

    __table_args__ = (
        Index("ix_incidents_status_started", "status", "started_at"),
        Index(
            "ix_incidents_active",
            "status",
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_incidents_summary_pending",
            "summary_next_attempt_at",
            postgresql_where=text("summary_status IN ('pending', 'retrying', 'processing')"),
        ),
        Index(
            "ix_incidents_sla_open",
            "sla_due_at",
            postgresql_where=text("workflow_status NOT IN ('resolved', 'ignored')"),
        ),
    )


class IncidentMember(Base):
    """One alert's durable, referentially intact incident membership."""

    __tablename__ = "incident_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("webhook_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: utcnow())

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_incident_members_event_id"),
        Index("ix_incident_members_incident_timestamp", "incident_id", "event_timestamp"),
    )
