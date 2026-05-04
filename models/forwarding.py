from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from db.session import Base, SerializerMixin


class ForwardRule(Base, SerializerMixin):
    """转发规则配置"""

    __tablename__ = "forward_rules"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)

    match_importance = Column(String(50), default="")
    match_duplicate = Column(String(20), default="all")
    match_source = Column(String(200), default="")

    target_type = Column(String(20), nullable=False)
    target_url = Column(String(500), default="")
    target_name = Column(String(100), default="")

    stop_on_match = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (Index("idx_forward_rules_priority", "priority"),)


class FailedForward(Base, SerializerMixin):
    """转发失败记录 - 用于重试补偿机制"""

    __tablename__ = "failed_forwards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    webhook_event_id = Column(Integer, nullable=False, index=True)
    forward_rule_id = Column(Integer, nullable=True)
    target_url = Column(String(500), nullable=False)
    target_type = Column(String(20), nullable=False)
    status = Column(String(20), default="pending")
    failure_reason = Column(String(500))
    error_message = Column(Text)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    next_retry_at = Column(DateTime)
    last_retry_at = Column(DateTime)
    forward_data = Column(JSONB)
    forward_headers = Column(JSONB)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_failed_status_retry", "status", "next_retry_at"),
        Index(
            "idx_failed_forwards_pending", "next_retry_at", postgresql_where=text("status IN ('pending', 'retrying')")
        ),
        Index("idx_failed_webhook_event", "webhook_event_id"),
    )
