from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


@dataclass(frozen=True, slots=True)
class WebhookEventInput:
    source: str
    request_id: str | None = None
    client_ip: str | None = None
    timestamp: datetime | None = None
    raw_payload: bytes | None = None
    headers: Mapping[str, Any] | None = None
    parsed_data: Mapping[str, Any] | None = None
    alert_hash: str | None = None
    dedup_key: str | None = None
    ai_analysis: Mapping[str, Any] | None = None
    importance: str | None = None
    processing_status: str = "received"
    retry_count: int = 0
    next_retry_at: datetime | None = None
    failure_reason: str | None = None
    error_message: str | None = None
    forward_status: str | None = None
    prev_alert_id: int | None = None
    is_duplicate: bool = False
    duplicate_of: int | None = None
    duplicate_count: int = 1
    last_notified_at: datetime | None = None


class WebhookEvent(Base):
    """Webhook event model"""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(50))
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: utcnow(), index=True)

    raw_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    headers: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    parsed_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    alert_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    dedup_key: Mapped[str | None] = mapped_column(String(64), index=True)

    ai_analysis: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    importance: Mapped[str | None] = mapped_column(String(20))

    processing_status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    forward_status: Mapped[str | None] = mapped_column(String(20))

    prev_alert_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("webhook_events.id", ondelete="SET NULL"), nullable=True
    )

    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("webhook_events.id", ondelete="SET NULL"), index=True
    )
    duplicate_count: Mapped[int] = mapped_column(Integer, default=1)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow(), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow(), onupdate=lambda: utcnow(), server_default=func.now())

    __table_args__ = (
        Index("idx_hash_timestamp", "alert_hash", "timestamp"),
        Index("idx_dedup_key_timestamp", "dedup_key", "timestamp"),
        # Partial index for the dead-letter list/count queries, which filter on
        # processing_status='dead_letter' (otherwise a full sequential scan of a
        # large events table). Mirrors the pending partial indexes. Leads with
        # source so source-filtered dead-letter views also use it, then id for
        # the id-desc ordering.
        Index(
            "idx_webhook_events_dead_letter",
            "source",
            "id",
            postgresql_where=text("processing_status = 'dead_letter'"),
        ),
    )

    def fill_fields(self, data: WebhookEventInput) -> None:
        """Fill the mutable event columns through an explicit typed surface."""
        self.source = data.source
        self.request_id = data.request_id
        self.client_ip = data.client_ip
        self.timestamp = data.timestamp or self.timestamp or utcnow()
        self.raw_payload = data.raw_payload
        self.headers = dict(data.headers) if data.headers is not None else None
        self.parsed_data = dict(data.parsed_data) if data.parsed_data is not None else None
        self.alert_hash = data.alert_hash
        self.dedup_key = data.dedup_key
        self.ai_analysis = dict(data.ai_analysis) if data.ai_analysis is not None else None
        self.importance = data.importance
        self.processing_status = data.processing_status
        self.retry_count = data.retry_count
        self.next_retry_at = data.next_retry_at
        self.failure_reason = data.failure_reason
        self.error_message = data.error_message
        self.forward_status = data.forward_status
        self.prev_alert_id = data.prev_alert_id
        self.is_duplicate = data.is_duplicate
        self.duplicate_of = data.duplicate_of
        self.duplicate_count = data.duplicate_count
        self.last_notified_at = data.last_notified_at
        if not self.created_at:
            self.created_at = utcnow()


class ArchivedWebhookEvent(Base):
    """Archived webhook event, preserving a complete snapshot before deletion from the live table."""

    __tablename__ = "archived_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(50))
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    raw_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    headers: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    parsed_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    alert_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # Preserved so a dedup chain can be reconstructed from the archive for
    # forensics; older archived rows predate this column and are left NULL.
    dedup_key: Mapped[str | None] = mapped_column(String(64))

    ai_analysis: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    importance: Mapped[str | None] = mapped_column(String(20))

    processing_status: Mapped[str | None] = mapped_column(String(20))
    retry_count: Mapped[int | None] = mapped_column(Integer)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    forward_status: Mapped[str | None] = mapped_column(String(20))

    prev_alert_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_duplicate: Mapped[bool | None] = mapped_column(Boolean)
    duplicate_of: Mapped[int | None] = mapped_column(Integer)
    duplicate_count: Mapped[int | None] = mapped_column(Integer)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: utcnow(), index=True)

    __table_args__ = (Index("idx_archived_hash_timestamp", "alert_hash", "timestamp"),)
