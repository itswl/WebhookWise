"""Team activity log — records who changed silences, rules, and incidents."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class AuditLog(Base):
    """Transactional record of a state-changing operation.

    Written in the same transaction as the business change so the activity view
    cannot claim an operation committed when it did not.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # What was changed: "silence" | "forward_rule" | "incident"
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    resource_id: Mapped[int | None] = mapped_column(Integer)
    resource_name: Mapped[str | None] = mapped_column(String(200))

    # What happened: "created" | "updated" | "deleted" | "closed" | "reopened"
    action: Mapped[str] = mapped_column(String(20), nullable=False)

    # Human-readable summary line for the activity feed.
    summary: Mapped[str] = mapped_column(String(500), nullable=False)

    # Who did it — captured from the dashboard's auth context.
    actor: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: utcnow(), index=True)

    __table_args__ = (Index("ix_audit_log_type_created", "resource_type", "created_at"),)
