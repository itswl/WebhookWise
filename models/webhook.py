from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import datetime
from typing import Any

import orjson
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from adapters.normalized import extract_alert_identity
from core.compression import compress_payload
from db.session import Base

_logger = logging.getLogger(__name__)


class WebhookEvent(Base):
    """Webhook 事件模型"""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(50))
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, index=True)

    raw_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    headers: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    parsed_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    alert_hash: Mapped[str | None] = mapped_column(String(64), index=True)

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
    duplicate_of: Mapped[int | None] = mapped_column(Integer, ForeignKey("webhook_events.id", ondelete="SET NULL"))
    duplicate_count: Mapped[int] = mapped_column(Integer, default=1)
    beyond_window: Mapped[bool] = mapped_column(Boolean, default=False)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (Index("idx_hash_timestamp", "alert_hash", "timestamp"),)

    @staticmethod
    def generate_hash(data: dict[str, Any], source: str) -> str:
        """生成告警哈希"""
        identity = extract_alert_identity(data)
        key_fields: dict[str, object]
        if identity:
            key_fields = dict(identity)
            key_fields.setdefault("source", source.strip().lower())
        else:
            _logger.warning("缺少 adapter 产出的告警 identity，使用完整 payload hash 兜底 source=%s", source)
            key_fields = {"source": source.strip().lower(), "payload": data}

        return hashlib.sha256(orjson.dumps(key_fields, option=orjson.OPT_SORT_KEYS)).hexdigest()

    def fill_fields(self, **kwargs: object) -> None:
        """统一填充字段"""
        valid_fields = set(type(self).__mapper__.column_attrs.keys())
        unknown_fields = sorted(k for k in kwargs if k not in valid_fields)
        if unknown_fields:
            raise ValueError(f"未知 WebhookEvent 字段: {','.join(unknown_fields)}")
        for k, v in kwargs.items():
            if k == "raw_payload" and isinstance(v, bytes):
                with contextlib.suppress(Exception):
                    v = compress_payload(v.decode("utf-8"))
            if k == "headers" and isinstance(v, dict):
                v = dict(v)
            setattr(self, k, v)
        if not self.timestamp:
            self.timestamp = datetime.now()
        if not self.created_at:
            self.created_at = datetime.now()
        self.updated_at = datetime.now()


class ArchivedWebhookEvent(Base):
    """Webhook 事件归档表。"""

    __tablename__ = "archived_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(50))
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    raw_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    headers: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    parsed_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    alert_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    ai_analysis: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    importance: Mapped[str | None] = mapped_column(String(20), index=True)
    forward_status: Mapped[str | None] = mapped_column(String(20))
    is_duplicate: Mapped[bool | None] = mapped_column(Boolean)
    duplicate_of: Mapped[int | None] = mapped_column(Integer)
    duplicate_count: Mapped[int | None] = mapped_column(Integer)
    beyond_window: Mapped[bool | None] = mapped_column(Boolean)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now, index=True)

    __table_args__ = (Index("idx_archived_hash_timestamp", "alert_hash", "timestamp"),)
