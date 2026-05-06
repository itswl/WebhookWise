from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base, SerializerMixin


class ForwardRule(Base, SerializerMixin):
    """转发规则配置"""

    __tablename__ = "forward_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    match_importance: Mapped[str] = mapped_column(String(50), default="")
    match_duplicate: Mapped[str] = mapped_column(String(20), default="all")
    match_source: Mapped[str] = mapped_column(String(200), default="")

    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_url: Mapped[str] = mapped_column(String(500), default="")
    target_name: Mapped[str] = mapped_column(String(100), default="")

    stop_on_match: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (Index("idx_forward_rules_priority", "priority"),)


class FailedForward(Base, SerializerMixin):
    """转发失败记录 - 用于重试补偿机制"""

    __tablename__ = "failed_forwards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    webhook_event_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    forward_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_url: Mapped[str] = mapped_column(String(500), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    forward_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    forward_headers: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_failed_status_retry", "status", "next_retry_at"),
        Index(
            "idx_failed_forwards_pending", "next_retry_at", postgresql_where=text("status IN ('pending', 'retrying')")
        ),
        Index("idx_failed_webhook_event", "webhook_event_id"),
    )
