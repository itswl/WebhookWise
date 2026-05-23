from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base


class ForwardRule(Base):
    """转发规则配置"""

    __tablename__ = "forward_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    match_importance: Mapped[str] = mapped_column(String(50), default="")
    match_duplicate: Mapped[str] = mapped_column(String(20), default="all")
    match_source: Mapped[str] = mapped_column(String(200), default="")
    match_payload: Mapped[str] = mapped_column(String(512), default="")

    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_url: Mapped[str] = mapped_column(String(500), default="")
    target_name: Mapped[str] = mapped_column(String(100), default="")

    stop_on_match: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (Index("idx_forward_rules_priority", "priority"),)


class ForwardOutbox(Base):
    """Transactional forwarding intent.

    Pipeline code writes this row in the same DB transaction as the processed
    webhook state. A worker performs the network side effect later.
    """

    __tablename__ = "forward_outboxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    webhook_event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    original_event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("webhook_events.id", ondelete="SET NULL"), nullable=True
    )
    forward_rule_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("forward_rules.id", ondelete="SET NULL"), nullable=True
    )

    rule_name: Mapped[str] = mapped_column(String(100), default="")
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_url: Mapped[str] = mapped_column(String(500), default="")
    target_name: Mapped[str] = mapped_column(String(100), default="")
    is_periodic_reminder: Mapped[bool] = mapped_column(Boolean, default=False)

    channel_name: Mapped[str] = mapped_column(String(32), default="")
    event_type: Mapped[str] = mapped_column(String(32), default="")

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)

    forward_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    analysis_result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    formatted_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    response_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index(
            "idx_forward_outboxes_pending",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'retrying')"),
        ),
        Index("idx_forward_outboxes_event", "webhook_event_id"),
    )
