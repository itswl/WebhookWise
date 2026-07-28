"""Managed inbound source connections and scoped webhook credentials."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class SourceConnection(Base):
    """One operator-managed inbound alert source.

    Only a SHA-256 digest and a short display hint are persisted for the scoped
    source credential. The plaintext token is returned once on creation or
    rotation and cannot be recovered from the database.
    """

    __tablename__ = "source_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hint: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    first_event_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_request_id: Mapped[str | None] = mapped_column(String(64))
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    last_auth_failure_at: Mapped[datetime | None] = mapped_column(DateTime)
    auth_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    schema_change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    schema_changed_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: utcnow(),
        onupdate=lambda: utcnow(),
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_source_connections_public_id"),
        Index("ix_source_connections_enabled_last_event", "enabled", "last_event_at"),
        Index(
            "ix_source_connections_active_type",
            "source_type",
            postgresql_where=text("enabled = true"),
        ).ddl_if(dialect="postgresql"),
    )
