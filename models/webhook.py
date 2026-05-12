from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

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
from adapters.summary_extractors import extract_summary_fields
from core.compression import compress_payload, decompress_payload
from db.session import Base, SerializerMixin

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pydantic import BaseModel


class WebhookEvent(Base, SerializerMixin):
    """Webhook 事件模型"""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(50))
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, index=True)

    raw_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    headers: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    parsed_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    alert_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    ai_analysis: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    importance: Mapped[str | None] = mapped_column(String(20), index=True)

    processing_status: Mapped[str] = mapped_column(String(20), default="received", nullable=False, index=True)
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

    __table_args__ = (
        Index("idx_unique_alert_hash_original", "alert_hash", unique=True, postgresql_where=(is_duplicate.is_(False))),
        Index("idx_hash_timestamp", "alert_hash", "timestamp"),
        Index("idx_importance_timestamp", "importance", "timestamp"),
        Index("idx_duplicate_lookup", "alert_hash", "is_duplicate", "timestamp"),
        Index("idx_status_created", "processing_status", "created_at"),
        Index("idx_retry_due", "next_retry_at", postgresql_where=(processing_status == "retry")),
        Index("idx_source_timestamp_id", "source", "timestamp", "id"),
    )

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
        for k, v in kwargs.items():
            if k == "raw_payload" and isinstance(v, bytes):
                with contextlib.suppress(Exception):
                    v = compress_payload(v.decode("utf-8"))
            if k == "headers" and isinstance(v, dict):
                v = dict(v)
            if hasattr(self, k):
                setattr(self, k, v)
        if not self.timestamp:
            self.timestamp = datetime.now()
        if not self.created_at:
            self.created_at = datetime.now()
        self.updated_at = datetime.now()

    def to_summary_dict(self) -> dict[str, object]:
        ai_analysis = self.ai_analysis
        summary = ai_analysis.get("summary", "") if ai_analysis else None
        alert_info = extract_summary_fields(self.source, self.parsed_data)

        res = super().to_dict()
        res.update(
            {
                "summary": summary,
                "alert_info": alert_info,
                "timestamp": self.timestamp.isoformat() if self.timestamp else None,
                "created_at": self.created_at.isoformat() if self.created_at else None,
            }
        )
        return res

    def to_dict(self, schema_cls: type[BaseModel] | None = None) -> dict[str, object]:
        if schema_cls:
            return super().to_dict(schema_cls)

        res = super().to_dict()
        res["raw_payload"] = decompress_payload(self.raw_payload)

        for k in ["timestamp", "created_at", "updated_at", "last_notified_at", "next_retry_at"]:
            val = getattr(self, k, None)
            if isinstance(val, datetime):
                res[k] = val.isoformat()

        # 计算字段
        is_dup = self.is_duplicate
        beyond = self.beyond_window
        res["duplicate_type"] = ("beyond_window" if beyond else "within_window") if is_dup else "new"

        return res


class ArchivedWebhookEvent(Base, SerializerMixin):
    """已归档的 Webhook 事件模型（历史备份）"""

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
