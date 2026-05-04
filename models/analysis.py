from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
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

from db.session import Base, SerializerMixin


class AIUsageLog(Base, SerializerMixin):
    """AI 调用成本追踪"""

    __tablename__ = "ai_usage_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=func.now(), index=True)
    model = Column(String(100))
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    cost_estimate = Column(Float, default=0.0)
    cache_hit = Column(Boolean, default=False)
    route_type = Column(String(20))
    alert_hash = Column(String(64), index=True)
    source = Column(String(100))

    __table_args__ = (Index("idx_usage_timestamp_route", "timestamp", "route_type"),)


class DeepAnalysis(Base, SerializerMixin):
    """深度分析历史记录"""

    __tablename__ = "deep_analyses"

    __table_args__ = (Index("idx_deep_analyses_pending", "created_at", postgresql_where=text("status = 'pending'")),)

    id = Column(Integer, primary_key=True)
    webhook_event_id = Column(Integer, nullable=False, index=True)
    engine = Column(String(20), default="local")
    user_question = Column(Text, default="")
    analysis_result = Column(JSONB)
    duration_seconds = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.now)
    openclaw_run_id = Column(String(64), index=True)
    openclaw_session_key = Column(String(200))
    status = Column(String(20), default="completed", index=True)
