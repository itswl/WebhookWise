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
    # No standalone index: every resource_type filter also orders by created_at
    # (api/v1/activity.py, services/operations/action_center.py), which is
    # exactly ix_audit_log_type_created's leading column.
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_id: Mapped[int | None] = mapped_column(Integer)
    resource_name: Mapped[str | None] = mapped_column(String(200))

    # What happened: "created" | "updated" | "deleted" | "closed" | "reopened"
    action: Mapped[str] = mapped_column(String(20), nullable=False)

    # Human-readable summary line for the activity feed.
    summary: Mapped[str] = mapped_column(String(500), nullable=False)

    # Who did it — self-declared by the caller (request body / client default).
    # There are no per-user accounts: every writer holds the shared admin key,
    # so this is attribution the operator asserts, not an authenticated
    # identity. Do not treat it as one.
    actor: Mapped[str | None] = mapped_column(String(100))

    # Indexed on its own as well as in the composite below: the activity feed's
    # default query has no resource_type filter, so a composite that leads with
    # resource_type cannot serve its created_at DESC ordering.
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: utcnow(), index=True)

    __table_args__ = (Index("ix_audit_log_type_created", "resource_type", "created_at"),)
