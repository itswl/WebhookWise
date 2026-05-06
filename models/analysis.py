from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base, SerializerMixin


class AIUsageLog(Base, SerializerMixin):
    """AI 调用成本追踪"""

    __tablename__ = "ai_usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime, default=func.now(), index=True)
    model: Mapped[str | None] = mapped_column(String(100))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    route_type: Mapped[str | None] = mapped_column(String(20))
    alert_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    source: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (Index("idx_usage_timestamp_route", "timestamp", "route_type"),)


class DeepAnalysis(Base, SerializerMixin):
    """深度分析历史记录"""

    __tablename__ = "deep_analyses"

    __table_args__ = (Index("idx_deep_analyses_pending", "created_at", postgresql_where=text("status = 'pending'")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    webhook_event_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String(20), default="local")
    user_question: Mapped[str] = mapped_column(Text, default="")
    analysis_result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    openclaw_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    openclaw_session_key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="completed", index=True)
