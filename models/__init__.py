"""
数据库模型定义
"""

import logging
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Index, Integer, String, Text, create_engine, func, text
from sqlalchemy.orm import sessionmaker

from core.config import Config
from db.session import Base

# 模块 logger
_logger = logging.getLogger(__name__)

# 全局数据库引擎（单例）
_engine = None
_session_factory = None


class WebhookEvent(Base):
    """Webhook 事件模型"""

    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(100), nullable=False, index=True)
    client_ip = Column(String(50))
    timestamp = Column(DateTime, nullable=False, default=datetime.now, index=True)

    # 原始数据
    raw_payload = Column(Text)
    headers = Column(JSON)
    parsed_data = Column(JSON)

    # 告警去重标识 (基于关键字段的哈希值)
    alert_hash = Column(String(64), index=True)

    # AI 分析结果
    ai_analysis = Column(JSON)
    importance = Column(String(20), index=True)  # high, medium, low

    # 转发状态
    forward_status = Column(String(20))  # success, failed, skipped

    # 是否为重复告警
    is_duplicate = Column(Integer, default=0)  # 0: 新告警, 1: 重复告警
    duplicate_of = Column(Integer)  # 如果是重复告警，指向原始告警的ID
    duplicate_count = Column(Integer, default=1)  # 重复次数
    beyond_window = Column(Integer, default=0)  # 0: 窗口内, 1: 窗口外
    last_notified_at = Column(DateTime)  # 上次通知时间（用于周期性提醒）

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 复合索引：优化去重查询性能
    __table_args__ = (
        Index("idx_hash_timestamp", "alert_hash", "timestamp"),
        Index("idx_importance_timestamp", "importance", "timestamp"),
        Index("idx_duplicate_lookup", "alert_hash", "is_duplicate", "timestamp"),
    )

    def to_summary_dict(self):
        """返回摘要信息（用于列表显示，减少数据传输量）"""
        # 提取 AI 分析摘要
        summary = None
        if self.ai_analysis:
            summary = self.ai_analysis.get("summary", "")

        # 提取关键告警信息
        alert_info = {}
        if self.parsed_data and self.source == "mongodb":
            alert_info = {
                "host": self.parsed_data.get("监控项", {}).get("主机", "")
                if isinstance(self.parsed_data.get("监控项"), dict)
                else "",
                "metric": self.parsed_data.get("监控项", {}).get("监控项", "")
                if isinstance(self.parsed_data.get("监控项"), dict)
                else "",
                "value": self.parsed_data.get("当前值", ""),
            }
            # 可以添加其他来源的提取逻辑

        return {
            "id": self.id,
            "source": self.source,
            "client_ip": self.client_ip,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "importance": self.importance,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
            "duplicate_count": self.duplicate_count,
            "beyond_window": self.beyond_window,
            "forward_status": self.forward_status,
            "summary": summary,  # AI 摘要（而非完整分析）
            "alert_info": alert_info,  # 告警关键信息（而非完整 parsed_data）
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "prev_alert_id": None,  # 占位符，由 get_all_webhooks 填充
            "prev_alert_timestamp": None,  # 占位符，由 get_all_webhooks 填充
        }

    def to_dict(self):
        """转换为字典（完整数据，用于详情查看）"""
        return {
            "id": self.id,
            "source": self.source,
            "client_ip": self.client_ip,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "raw_payload": self.raw_payload,
            "headers": self.headers,
            "parsed_data": self.parsed_data,
            "alert_hash": self.alert_hash,
            "ai_analysis": self.ai_analysis,
            "importance": self.importance,
            "forward_status": self.forward_status,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
            "duplicate_count": self.duplicate_count,
            "beyond_window": self.beyond_window,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ArchivedWebhookEvent(Base):
    """已归档的 Webhook 事件模型（历史备份）"""

    __tablename__ = "archived_webhook_events"

    id = Column(Integer, primary_key=True)
    source = Column(String(100), nullable=False, index=True)
    client_ip = Column(String(50))
    timestamp = Column(DateTime, nullable=False, index=True)

    raw_payload = Column(Text)
    headers = Column(JSON)
    parsed_data = Column(JSON)
    alert_hash = Column(String(64), index=True)
    ai_analysis = Column(JSON)
    importance = Column(String(20), index=True)
    forward_status = Column(String(20))
    is_duplicate = Column(Integer)
    duplicate_of = Column(Integer)
    duplicate_count = Column(Integer)
    beyond_window = Column(Integer)
    last_notified_at = Column(DateTime)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    archived_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (Index("idx_archived_hash_timestamp", "alert_hash", "timestamp"),)


class AnalysisCache(Base):
    """
    AI 分析结果缓存

    用于存储 AI 分析结果，避免对相同告警重复调用 AI。
    缓存 key 基于 alert_hash 生成。
    """

    __tablename__ = "analysis_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(String(128), unique=True, nullable=False, index=True)  # 基于 alert_hash
    analysis_result = Column(Text, nullable=False)  # JSON 格式的分析结果
    hit_count = Column(Integer, default=0)  # 缓存命中次数
    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=False)

    def is_expired(self) -> bool:
        """检查缓存是否已过期"""
        return datetime.now() > self.expires_at if self.expires_at else True


class AIUsageLog(Base):
    """
    AI 调用成本追踪

    记录每次 AI 分析的调用信息，用于成本追踪和用量统计。
    """

    __tablename__ = "ai_usage_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=func.now(), index=True)
    model = Column(String(100))  # 使用的模型名称
    tokens_in = Column(Integer, default=0)  # 输入 token 数
    tokens_out = Column(Integer, default=0)  # 输出 token 数
    cost_estimate = Column(Float, default=0.0)  # 估算成本（美元）
    cache_hit = Column(Boolean, default=False)  # 是否命中缓存
    route_type = Column(String(20))  # 'ai', 'rule', 'cache'
    alert_hash = Column(String(64), index=True)  # 关联的告警哈希
    source = Column(String(100))  # 告警来源

    # 复合索引：优化统计查询性能
    __table_args__ = (Index("idx_usage_timestamp_route", "timestamp", "route_type"),)


# 数据库连接（单例模式）
def get_engine():
    """获取数据库引擎（单例）"""
    global _engine
    if _engine is None:
        _logger.info(f"[DB] 正在初始化数据库连接池: {Config.DATABASE_URL.split('@')[-1]}")
        _engine = create_engine(
            Config.DATABASE_URL,
            echo=False,
            pool_pre_ping=True,  # 连接前检查有效性
            pool_size=Config.DB_POOL_SIZE,  # 连接池大小
            max_overflow=Config.DB_MAX_OVERFLOW,  # 最大溢出连接
            pool_recycle=Config.DB_POOL_RECYCLE,  # 连接回收时间
            pool_timeout=Config.DB_POOL_TIMEOUT,  # 连接超时
        )
    return _engine


def get_session():
    """获取数据库会话"""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine())
    return _session_factory()


@contextmanager
def session_scope():
    """数据库会话上下文管理器，自动处理提交和回滚"""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """初始化数据库表"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _logger.info("数据库表初始化完成")


def test_db_connection() -> bool:
    """
    测试数据库连接

    Returns:
        bool: 连接成功返回 True，失败返回 False
    """
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        _logger.info("数据库连接测试成功")
        return True
    except Exception as e:
        _logger.error(f"数据库连接失败: {e}")
        return False


class RemediationExecution(Base):
    """
    Runbook 执行记录

    记录每次 Runbook 执行的详细信息，包括执行状态、步骤日志等。
    """

    __tablename__ = "remediation_execution"

    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(String(64), unique=True, nullable=False, index=True)  # UUID
    runbook_name = Column(String(200), nullable=False)
    trigger_alert_id = Column(Integer, nullable=True)  # 关联 WebhookEvent.id
    trigger_alert_hash = Column(String(64))
    status = Column(
        String(30), default="pending"
    )  # pending/awaiting_approval/running/success/failed/rolled_back/dry_run_complete
    steps_log = Column(Text, default="[]")  # JSON 格式的执行步骤日志
    dry_run = Column(Boolean, default=False)
    started_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    # 索引：优化按状态和时间查询
    __table_args__ = (Index("idx_remediation_status_time", "status", "started_at"),)

    def to_dict(self):
        """转换为字典"""
        import json

        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "runbook_name": self.runbook_name,
            "trigger_alert_id": self.trigger_alert_id,
            "trigger_alert_hash": self.trigger_alert_hash,
            "status": self.status,
            "steps_log": json.loads(self.steps_log) if self.steps_log else [],
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_message": self.error_message,
        }


class ForwardRule(Base):
    """转发规则配置"""

    __tablename__ = "forward_rules"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)  # 规则名称
    enabled = Column(Boolean, default=True)  # 是否启用
    priority = Column(Integer, default=0)  # 规则优先级（越大越先匹配）

    # 匹配条件
    match_importance = Column(String(50), default="")  # high,medium,low（逗号分隔，空=全部匹配）
    match_duplicate = Column(String(20), default="all")  # new/duplicate/beyond_window/all
    match_source = Column(String(200), default="")  # 来源匹配（逗号分隔列表，空=全部）

    # 转发目标
    target_type = Column(String(20), nullable=False)  # feishu / openclaw / webhook
    target_url = Column(String(500), default="")  # 飞书/webhook 的 URL
    target_name = Column(String(100), default="")  # 显示名称

    # 行为配置
    stop_on_match = Column(Boolean, default=False)  # 匹配后是否停止后续规则

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (Index("idx_forward_rules_priority", "priority"),)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "match_importance": self.match_importance,
            "match_duplicate": self.match_duplicate,
            "match_source": self.match_source,
            "target_type": self.target_type,
            "target_url": self.target_url,
            "target_name": self.target_name,
            "stop_on_match": self.stop_on_match,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DeepAnalysis(Base):
    """深度分析历史记录"""

    __tablename__ = "deep_analyses"

    id = Column(Integer, primary_key=True)
    webhook_event_id = Column(Integer, nullable=False, index=True)  # 关联告警 ID
    engine = Column(String(20), default="local")  # local / openclaw
    user_question = Column(Text, default="")  # 用户输入的问题
    analysis_result = Column(JSON)  # 完整分析结果 JSON
    duration_seconds = Column(Float, default=0)  # 分析耗时（秒）
    created_at = Column(DateTime, default=datetime.now)
    openclaw_run_id = Column(String(64), index=True)  # OpenClaw runId
    openclaw_session_key = Column(String(200))  # OpenClaw sessionKey（用于轮询）
    status = Column(String(20), default="completed", index=True)  # pending / completed / failed

    def to_dict(self):
        return {
            "id": self.id,
            "webhook_event_id": self.webhook_event_id,
            "engine": self.engine,
            "user_question": self.user_question,
            "analysis_result": self.analysis_result,
            "duration_seconds": self.duration_seconds,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "openclaw_run_id": self.openclaw_run_id,
            "openclaw_session_key": self.openclaw_session_key,
            "status": self.status or "completed",
        }


if __name__ == "__main__":
    init_db()
