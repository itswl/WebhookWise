"""Incident model — groups related alerts into an operational incident."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, text
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

    ``member_ids`` is a JSONB integer array of ``webhook_events.id`` values —
    kept in the same row for cheap "show me the incident timeline" reads without
    a join table. The count is denormalized into ``alert_count`` so the list
    page doesn't unpack the array.
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

    # Ordered list of webhook_event ids that belong to this incident (newest last).
    member_ids: Mapped[list[int] | None] = mapped_column(JSONB)

    # LLM-generated summary when the incident closes (null while active).
    summary_analysis: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=lambda: utcnow(), onupdate=lambda: utcnow()
    )

    __table_args__ = (
        Index("ix_incidents_status_started", "status", "started_at"),
        Index(
            "ix_incidents_active",
            "status",
            postgresql_where=text("status = 'active'"),
        ),
    )
